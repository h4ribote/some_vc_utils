import os
import subprocess
import sys

import pytest

from conftest import REPO
from settings import ConfigError, load_settings

FULL = {
    "SVU_DISCORD_TOKEN": "tok.en-123",
    "SVU_ADMIN_IDS": "111, 222",
    "SVU_VC_CLIENT_ID": "cid",
    "SVU_VC_CLIENT_SECRET": "csecret",
}


def write_env(path, values: dict, extra: str = "") -> None:
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()) + extra)


def test_missing_variables_are_all_named(tmp_path):
    with pytest.raises(ConfigError) as info:
        load_settings(tmp_path / "absent.env", environ={})
    report = info.value.report()
    for name in FULL:
        assert f"{name}: missing" in report
    assert len(info.value.problems) == 4


def test_values_from_env_file(tmp_path):
    env = tmp_path / ".env"
    write_env(env, FULL, '# comment\n\nexport SVU_BACKUP_RETENTION="7"\nSVU_LOG_LEVEL=debug\n')
    s = load_settings(env, environ={})
    assert s.discord_token == "tok.en-123"
    assert s.admin_ids == frozenset({111, 222})
    assert (s.vc_client_id, s.vc_client_secret) == ("cid", "csecret")
    assert s.backup_retention == 7
    assert s.backup_interval_hours == 6.0
    assert s.log_level == "DEBUG"


def test_environment_overrides_file(tmp_path):
    env = tmp_path / ".env"
    write_env(env, FULL)
    s = load_settings(env, environ={"SVU_DISCORD_TOKEN": "from-env", "SVU_ADMIN_IDS": "333"})
    assert s.discord_token == "from-env"
    assert s.admin_ids == frozenset({333})
    assert s.vc_client_id == "cid"


def test_invalid_values_reported_together(tmp_path):
    env = tmp_path / ".env"
    write_env(env, dict(FULL, SVU_ADMIN_IDS="12,abc,-5", SVU_BACKUP_RETENTION="0", SVU_BACKUP_INTERVAL_HOURS="x", SVU_LOG_LEVEL="loud"))
    with pytest.raises(ConfigError) as info:
        load_settings(env, environ={})
    text = "\n".join(info.value.problems)
    assert "SVU_ADMIN_IDS" in text and "'abc'" in text and "'-5'" in text
    assert "SVU_BACKUP_RETENTION: must be >= 1" in text
    assert "SVU_BACKUP_INTERVAL_HOURS: must be a number" in text
    assert "SVU_LOG_LEVEL" in text
    assert len(info.value.problems) == 4


def test_malformed_line_does_not_echo_content(tmp_path):
    env = tmp_path / ".env"
    write_env(env, FULL, "this-line-is-secret-ish\n")
    with pytest.raises(ConfigError) as info:
        load_settings(env, environ={})
    assert "line 5" in info.value.report()
    assert "secret-ish" not in info.value.report()


def test_secrets_not_in_repr(tmp_path):
    env = tmp_path / ".env"
    write_env(env, FULL)
    s = load_settings(env, environ={})
    assert "tok.en-123" not in repr(s) and "csecret" not in repr(s)
    assert set(s.secrets()) == {"tok.en-123", "csecret"}


def test_backup_cli_does_not_need_secrets(tmp_path):
    s = load_settings(tmp_path / "absent.env", environ={"SVU_BACKUP_RETENTION": "3"}, require_secrets=False)
    assert s.backup_retention == 3


def test_paths_do_not_depend_on_cwd(tmp_path):
    code = (
        "import settings, db_utils, backup, logging_setup\n"
        "print(settings.REPO_ROOT); print(settings.ENV_FILE); print(db_utils.Database().db_path); print(backup.BACKUP_DIR); print(logging_setup.LOG_DIR)\n"
    )
    env = dict(os.environ, PYTHONPATH=str(REPO))
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True, check=True).stdout.split()
    assert out == [str(REPO), str(REPO / ".env"), str(REPO / "data" / "database.db"), str(REPO / "backups"), str(REPO / "logs")]
    assert not (tmp_path / "data").exists()
