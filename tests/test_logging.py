import io
import logging

import pytest

from logging_setup import setup_logging
from settings import Settings


@pytest.fixture
def restore_root():
    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in saved[0]:
        root.addHandler(handler)
    root.setLevel(saved[1])


def test_file_and_stream_with_rotation_and_redaction(tmp_path, restore_root):
    settings = Settings(discord_token="SECRET.TOKEN", admin_ids=frozenset({1}), vc_client_id="cid", vc_client_secret="vc-secret", log_max_bytes=2048, log_backup_count=2)
    stream = io.StringIO()
    log_file = setup_logging(settings, tmp_path / "logs", stream)

    logging.getLogger("svu").info("token is %s", "SECRET.TOKEN")
    logging.getLogger("discord.gateway").info("gateway says hello")
    try:
        raise ValueError("bad vc-secret")
    except ValueError:
        logging.getLogger("svu").exception("failure")
    for i in range(100):
        logging.getLogger("svu").info("filler line %03d with some padding to force rotation", i)

    text = log_file.read_text() + "".join(p.read_text() for p in log_file.parent.glob("bot.log.*"))
    streamed = stream.getvalue()
    assert "gateway says hello" in streamed
    assert "token is [REDACTED]" in streamed
    assert "bad [REDACTED]" in streamed
    for output in (text, streamed):
        assert "SECRET.TOKEN" not in output and "vc-secret" not in output
    assert sorted(p.name for p in log_file.parent.iterdir()) == ["bot.log", "bot.log.1", "bot.log.2"]
    assert log_file.stat().st_size <= 2048
