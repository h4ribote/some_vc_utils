"""Runs the real svuctl against a throwaway copy of the repository with tests/fake_bot.py as the bot.

The copy is driven through two test hooks read by svuctl: SVU_TEST_BOT_CMD replaces the bot command line, and SVU_TEST_RUN_USER makes a root invocation run as "nobody" instead of svu.
"""

import os
import pwd
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from conftest import REPO

IS_ROOT = os.geteuid() == 0
RUN_AS = "nobody" if IS_ROOT else pwd.getpwuid(os.getuid()).pw_name


@pytest.fixture
def repo():
    root = Path(tempfile.mkdtemp(prefix="svuctl-test-", dir="/tmp"))
    root.chmod(0o755)
    for name in ["svuctl", "requirements.txt", *[p.name for p in REPO.glob("*.py")]]:
        shutil.copy2(REPO / name, root / name)
    shutil.copytree(REPO / "virtualcrypto", root / "virtualcrypto", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO / "deploy", root / "deploy")
    shutil.copy2(REPO / "tests" / "fake_bot.py", root / "fake_bot.py")
    state = root / "fakestate"
    state.mkdir()
    state.chmod(0o777)
    (state / "mode").write_text("run")
    (state / "mode").chmod(0o666)
    yield root
    if (root / "run" / "svu.pid").exists():
        svuctl(root, "stop", "--timeout", "5")
    shutil.rmtree(root, ignore_errors=True)


def env_for(root: Path, **extra) -> dict:
    env = dict(os.environ, SVU_TEST_BOT_CMD=f"python3 {root / 'fake_bot.py'} {root / 'fakestate'}", **extra)
    if IS_ROOT:
        env.setdefault("SVU_TEST_RUN_USER", RUN_AS)
    return env


def svuctl(root: Path, *args, **extra) -> subprocess.CompletedProcess:
    proc = subprocess.run([str(root / "svuctl"), *args], env=env_for(root, **extra), capture_output=True, text=True, timeout=120)
    print(f"$ svuctl {' '.join(args)}  -> exit {proc.returncode}\n{proc.stdout}{proc.stderr}")
    return proc


def prepare(root: Path, mode: str = "run") -> None:
    shutil.copy2(root / "deploy" / "env.example", root / ".env")
    for name in ("data", "logs", "backups", "run"):
        (root / name).mkdir(exist_ok=True)
        if IS_ROOT:
            pw = pwd.getpwnam(RUN_AS)
            os.chown(root / name, pw.pw_uid, pw.pw_gid)
    (root / "fakestate" / "mode").write_text(mode)


def events(root: Path) -> list[list[str]]:
    path = root / "fakestate" / "events"
    return [line.split() for line in path.read_text().splitlines()] if path.exists() else []


def wait_until(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def pid_alive(pid: int) -> bool:
    return Path(f"/proc/{pid}/cmdline").exists() and Path(f"/proc/{pid}/cmdline").read_bytes() != b""


def test_start_double_start_status_stop(repo):
    prepare(repo)
    started = svuctl(repo, "start")
    assert started.returncode == 0 and "started (supervisor pid" in started.stdout
    pid = int((repo / "run" / "svu.pid").read_text())
    assert pid_alive(pid)
    assert pwd.getpwuid(os.stat(f"/proc/{pid}").st_uid).pw_name == RUN_AS
    assert wait_until(lambda: len(events(repo)) == 1)

    again = svuctl(repo, "start")
    assert again.returncode == 1 and f"already running (supervisor pid {pid})" in again.stderr

    status = svuctl(repo, "status")
    assert status.returncode == 0
    assert f"running (supervisor pid {pid}, user {RUN_AS})" in status.stdout
    assert "restarts:   0" in status.stdout and "bot:        pid" in status.stdout

    stopped = svuctl(repo, "stop")
    assert stopped.returncode == 0 and "sent SIGTERM" in stopped.stdout and "stopped" in stopped.stdout
    assert [e[0] for e in events(repo)] == ["start", "term"]
    assert not (repo / "run" / "svu.pid").exists()
    assert not pid_alive(pid)

    after = svuctl(repo, "status")
    assert after.returncode == 3 and "not running" in after.stdout and "last exit 0" in after.stdout


def test_crash_restarts_with_backoff(repo):
    prepare(repo, "crash:3")
    assert svuctl(repo, "start").returncode == 0
    assert wait_until(lambda: len(events(repo)) == 4, timeout=20)
    times = [float(e[2]) for e in events(repo)]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert gaps[0] >= 0.9 and gaps[1] >= 1.9 and gaps[2] >= 3.9, gaps
    log = (repo / "logs" / "supervisor.log").read_text()
    assert "restart 1 in 1 s" in log and "restart 2 in 2 s" in log and "restart 3 in 4 s" in log
    assert "fake bot crashing" in log
    status = svuctl(repo, "status")
    assert "restarts:   3" in status.stdout and "last exit:  1" in status.stdout
    assert svuctl(repo, "stop").returncode == 0
    assert events(repo)[-1][0] == "term"


def test_clean_exit_and_config_error_stop_supervisor(repo):
    for mode, marker in (("exit0", "bot exited cleanly; supervisor stops"), ("config", "configuration error (exit 78); not restarting")):
        prepare(repo, mode)
        (repo / "fakestate" / "events").unlink(missing_ok=True)
        started = svuctl(repo, "start")
        # The supervisor may finish before start confirms it, which start reports with the exit code.
        assert started.returncode == 0 or "supervisor exited immediately with code 0" in started.stderr
        assert wait_until(lambda: not (repo / "run" / "svu.pid").exists())
        assert wait_until(lambda: "supervisor exiting" in (repo / "logs" / "supervisor.log").read_text().split(marker)[-1])
        assert len(events(repo)) == 1
        assert marker in (repo / "logs" / "supervisor.log").read_text()
        assert svuctl(repo, "status").returncode == 3


def test_stale_pid_file(repo):
    prepare(repo)
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        (repo / "run" / "svu.pid").write_text(f"{sleeper.pid}\n")
        status = svuctl(repo, "status")
        assert status.returncode == 3 and "stale pid file" in status.stdout
        started = svuctl(repo, "start")
        assert started.returncode == 0 and "removed stale pid file" in started.stdout
        assert int((repo / "run" / "svu.pid").read_text()) != sleeper.pid
        assert svuctl(repo, "stop").returncode == 0
        (repo / "run" / "svu.pid").write_text("garbage\n")
        stop = svuctl(repo, "stop")
        assert stop.returncode == 0 and "removed stale pid file" in stop.stdout and "not running" in stop.stdout
    finally:
        sleeper.kill()
        sleeper.wait()


def test_stop_escalates_to_sigkill(repo):
    prepare(repo, "ignore_term")
    assert svuctl(repo, "start").returncode == 0
    assert wait_until(lambda: len(events(repo)) == 1)
    time.sleep(0.3)
    bot_pid = int(events(repo)[0][1])
    stop = svuctl(repo, "stop", "--timeout", "2")
    assert stop.returncode == 1
    assert "did not stop within 2 s; sending SIGKILL" in stop.stdout
    assert not pid_alive(bot_pid)
    assert not (repo / "run" / "svu.pid").exists()


def test_supervisor_forwards_sigterm_from_outside(repo):
    prepare(repo)
    assert svuctl(repo, "start").returncode == 0
    assert wait_until(lambda: len(events(repo)) == 1)
    time.sleep(0.3)
    os.kill(int((repo / "run" / "svu.pid").read_text()), signal.SIGTERM)
    assert wait_until(lambda: not (repo / "run" / "svu.pid").exists())
    assert [e[0] for e in events(repo)] == ["start", "term"]


def test_backup_command(repo):
    prepare(repo)
    db = repo / "data" / "database.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    if IS_ROOT:
        pw = pwd.getpwnam(RUN_AS)
        os.chown(db, pw.pw_uid, pw.pw_gid)
    (repo / ".env").write_text((repo / ".env").read_text().replace("SVU_BACKUP_RETENTION=28", "SVU_BACKUP_RETENTION=2"))
    for _ in range(3):
        assert svuctl(repo, "backup").returncode == 0
    backups = sorted((repo / "backups").glob("database-*.db"))
    assert len(backups) == 2
    assert pwd.getpwuid(backups[0].stat().st_uid).pw_name == RUN_AS
    db.unlink()
    missing = svuctl(repo, "backup")
    assert missing.returncode == 1 and "database not found" in missing.stdout


def test_setup(repo):
    refused = svuctl(repo, "setup")
    assert refused.returncode == 1 and "cp " in refused.stderr and "env.example" in refused.stderr

    shutil.copy2(repo / "deploy" / "env.example", repo / ".env")
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    # A wrapper around the real venv's interpreter, so pip only confirms the pinned packages already installed there.
    (venv_bin / "python").write_text(f"#!/bin/sh\nexec {REPO / '.venv' / 'bin' / 'python'} \"$@\"\n")
    (venv_bin / "python").chmod(0o755)
    (repo / "data").mkdir()
    (repo / "data" / "database.db").write_bytes(b"")

    if IS_ROOT:
        missing = svuctl(repo, "setup", SVU_TEST_RUN_USER="svu-test-no-such-user")
        assert missing.returncode == 1 and "useradd --system" in missing.stderr

    done = svuctl(repo, "setup")
    assert done.returncode == 0, done.stderr
    for name, mode in (("data", 0o700), ("logs", 0o750), ("backups", 0o700), ("run", 0o750)):
        st = (repo / name).stat()
        assert st.st_mode & 0o777 == mode
        assert pwd.getpwuid(st.st_uid).pw_name == RUN_AS
    env_st = (repo / ".env").stat()
    assert env_st.st_mode & 0o777 == (0o400 if IS_ROOT else 0o600)
    assert pwd.getpwuid(env_st.st_uid).pw_name == RUN_AS
    assert (repo / "data" / "database.db").stat().st_mode & 0o777 == 0o600
    assert pwd.getpwuid(repo.stat().st_uid).pw_name == pwd.getpwuid(os.getuid()).pw_name
