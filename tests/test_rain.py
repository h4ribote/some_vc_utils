import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot_commands as cmds
import embedColour
from conftest import FakeVC, member


def interaction(user_id=1):
    it = MagicMock()
    it.user = SimpleNamespace(id=user_id, mention=f"<@{user_id}>")
    it.response.defer = AsyncMock()
    it.followup.send = AsyncMock()
    it.edit_original_response = AsyncMock()
    return it


@pytest.fixture
def approved(monkeypatch):
    monkeypatch.setattr(cmds, "wait_for_claim_approval", AsyncMock(return_value=True))


def run_rain(vc, members, amount=5):
    cmds.configure(vc, [])
    try:
        it = interaction()
        asyncio.run(cmds.rain.callback(it, "TST", amount, SimpleNamespace(members=members)))
        return it
    finally:
        cmds.configure(None, [])


def final_embed(it):
    return it.edit_original_response.await_args.kwargs["embeds"][-1]


def test_rain_all_paid(approved):
    vc = FakeVC()
    it = run_rain(vc, [member(i) for i in range(1, 4)])
    embed = final_embed(it)
    assert embed.title == "処理が完了しました"
    assert "3人のメンバーに **5 TST** を配布しました" in embed.description
    assert len(vc.payments) == 3


def test_rain_partial_failure_continues_and_reports(approved, caplog):
    vc = FakeVC(fail_ids={2, 4})
    members = [member(i) for i in range(1, 6)]
    it = run_rain(vc, members)
    assert [a[1] for a in vc.attempts] == [1, 2, 3, 4, 5]
    assert [p[1] for p in vc.payments] == [1, 3, 5]
    embed = final_embed(it)
    assert embed.title == "一部のメンバーへの配布に失敗しました"
    assert embed.colour == embedColour.Orange
    assert "5人中3人" in embed.description and "合計 **10 TST**" in embed.description
    assert embed.fields[0].value == "<@2> <@4>"
    assert "2 of 5 payments" in caplog.text and "2 (RuntimeError" in caplog.text and "4 (RuntimeError" in caplog.text


def test_rain_total_failure_is_error_coloured(approved):
    vc = FakeVC(fail_ids={1, 2})
    embed = final_embed(run_rain(vc, [member(1), member(2)]))
    assert embed.colour == embedColour.Error
    assert "2人中0人" in embed.description


def test_failed_member_list_fits_embed_field():
    members = [member(10**17 + i) for i in range(200)]
    text = cmds._mention_list(members)
    assert len(text) <= 1024
    shown = text.count("<@")
    assert text.endswith(f"...他{200 - shown}人")
    assert cmds._mention_list(members[:3]) == " ".join(m.mention for m in members[:3])
