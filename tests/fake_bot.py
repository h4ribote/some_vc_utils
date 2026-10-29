"""Stand-in bot for tests/test_svuctl.py, run by the supervisor through the SVU_TEST_BOT_CMD hook.

Usage: fake_bot.py <state-dir>. The file <state-dir>/mode selects the behaviour: "run" waits for SIGTERM, "crash:N" exits 1 on the first N starts, "exit0" exits 0, "config" exits 78, "ignore_term" ignores SIGTERM.
Every start and every SIGTERM is appended to <state-dir>/events as "<event> <pid> <unix time>".
"""

import os
import signal
import sys
import time
from pathlib import Path

state = Path(sys.argv[1])
events = state / "events"


def record(event: str) -> None:
    with open(events, "a") as fp:
        fp.write(f"{event} {os.getpid()} {time.time():.3f}\n")


def starts() -> int:
    return sum(1 for line in events.read_text().splitlines() if line.startswith("start ")) if events.exists() else 0


def on_term(signum, frame):
    record("term")
    sys.exit(0)


record("start")
mode = (state / "mode").read_text().strip()
if mode.startswith("crash:") and starts() <= int(mode.split(":")[1]):
    print("fake bot crashing", file=sys.stderr)
    sys.exit(1)
if mode == "exit0":
    sys.exit(0)
if mode == "config":
    sys.exit(78)
signal.signal(signal.SIGTERM, signal.SIG_IGN if mode == "ignore_term" else on_term)
while True:
    time.sleep(0.1)
