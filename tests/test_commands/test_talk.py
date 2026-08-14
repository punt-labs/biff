"""Humble-object tests for the shared talk command layer (biff.commands.talk).

Each action is called directly on a ``CliContext`` backed by a ``LocalRelay`` —
no subprocess, no NATS, no mocking (PL-TT-5).  ``LocalRelay``'s talk publish is
a no-op, so these cover the state-machine orchestration and the
``CommandResult`` contract; the transient publish-failure paths (which need an
injectable relay that raises on publish) live in
``tests/test_server/test_talk.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import biff.commands.talk as talk_commands
from biff.cli_session import CliContext
from biff.models import BiffConfig, UserSession
from biff.relay import LocalRelay
from biff.talk_types import TalkPhase

if TYPE_CHECKING:
    from pathlib import Path

    from biff.relay import Relay
    from biff.talk_types import PendingInvite

_KAI_KEY = "kai:kaihex01"
_ERIC_KEY = "eric:erichex1"


@pytest.fixture()
def relay(tmp_path: Path) -> LocalRelay:
    """Fresh LocalRelay backed by tmp_path — talk publish is a no-op here."""
    return LocalRelay(tmp_path)


@pytest.fixture()
def kai(relay: LocalRelay) -> CliContext:
    """CliContext for user 'kai' with a deterministic session key and tty name."""
    return CliContext(
        relay=relay,
        config=BiffConfig(user="kai", repo_name="test"),
        session_key=_KAI_KEY,
        user="kai",
        tty="kaihex01",
        tty_name="tty1",
    )


async def _register_eric(relay: Relay) -> None:
    """Register eric's session so kai can resolve and address it."""
    await relay.update_session(
        UserSession(user="eric", tty="erichex1", tty_name="tty2", repo="test")
    )


def _feed_invite_from_eric(kai: CliContext) -> PendingInvite:
    """Deliver an invite from eric and drain it into kai's pending set."""
    kai.talk.receive(
        {
            "type": "invite",
            "from": "eric",
            "from_tty": "tty2",
            "from_key": _ERIC_KEY,
            "body": "wants to talk",
            "to_key": _KAI_KEY,
        }
    )
    kai.talk.drain_idle()
    return kai.talk.pending_invites["eric"]


class TestInvite:
    async def test_idle_invite_sets_inviting_phase(self, kai: CliContext) -> None:
        result = await talk_commands.invite(
            kai,
            user="eric",
            relay_key=_ERIC_KEY,
            display="eric:tty2",
            resolve_tty="tty2",
            message="",
        )
        assert not result.error
        assert "invite sent to eric:tty2" in result.text.lower()
        assert kai.talk.phase is TalkPhase.INVITING
        assert kai.talk.partner_key == _ERIC_KEY

    async def test_invite_refused_when_already_in_talk(self, kai: CliContext) -> None:
        kai.talk.begin_connected(
            partner="jo", partner_tty="tty9", partner_key="jo:johex001"
        )
        result = await talk_commands.invite(
            kai,
            user="eric",
            relay_key=_ERIC_KEY,
            display="eric:tty2",
            resolve_tty="tty2",
            message="",
        )
        assert result.error
        assert "already in a talk" in result.text.lower()
        assert kai.talk.partner_key == "jo:johex001"  # live talk untouched


class TestAcceptInvite:
    async def test_accept_connects_and_consumes(self, kai: CliContext) -> None:
        pending = _feed_invite_from_eric(kai)
        result = await talk_commands.accept_invite(
            kai,
            user="eric",
            pending=pending,
            relay_key=_ERIC_KEY,
            display="eric:tty2",
            resolve_tty="tty2",
            message="",
        )
        assert not result.error
        assert "connected to eric:tty2" in result.text.lower()
        assert kai.talk.phase is TalkPhase.CONNECTED
        assert kai.talk.partner_key == _ERIC_KEY
        assert kai.talk.pending_invites == {}  # one-shot

    async def test_accept_with_opening_message_notes_it(self, kai: CliContext) -> None:
        pending = _feed_invite_from_eric(kai)
        result = await talk_commands.accept_invite(
            kai,
            user="eric",
            pending=pending,
            relay_key=_ERIC_KEY,
            display="eric:tty2",
            resolve_tty="tty2",
            message="on my way",
        )
        lowered = result.text.lower()
        assert "sent:" in lowered
        assert "on my way" in lowered
        assert kai.talk.phase is TalkPhase.CONNECTED

    async def test_accept_refused_when_connected_elsewhere(
        self, kai: CliContext
    ) -> None:
        """Accepting eric while connected to a different peer must not clobber it."""
        pending = _feed_invite_from_eric(kai)
        kai.talk.begin_connected(
            partner="jo", partner_tty="tty9", partner_key="jo:johex001"
        )
        result = await talk_commands.accept_invite(
            kai,
            user="eric",
            pending=pending,
            relay_key=_ERIC_KEY,
            display="eric:tty2",
            resolve_tty="tty2",
            message="",
        )
        assert result.error
        assert "already in a talk" in result.text.lower()
        assert kai.talk.partner_key == "jo:johex001"  # A kept
        assert "eric" in kai.talk.pending_invites  # invite not consumed


class TestSendLine:
    async def test_send_message_while_connected(self, kai: CliContext) -> None:
        kai.talk.begin_connected(
            partner="eric", partner_tty="tty2", partner_key=_ERIC_KEY
        )
        result = await talk_commands.send_line(
            kai, to_key=_ERIC_KEY, display="eric:tty2", message="hello there"
        )
        assert not result.error
        lowered = result.text.lower()
        assert "sent to eric:tty2:" in lowered
        assert "hello there" in lowered

    async def test_send_confirmation_wraps_cjk_within_the_table_width(
        self, kai: CliContext
    ) -> None:
        """The echoed message must wrap, not overflow, for CJK text."""
        from biff._formatting import TABLE_WIDTH, visible_width

        kai.talk.begin_connected(
            partner="eric", partner_tty="tty2", partner_key=_ERIC_KEY
        )
        cjk_message = "这是一段很长的中文文本用来测试自动换行是否正常工作" * 3
        result = await talk_commands.send_line(
            kai, to_key=_ERIC_KEY, display="eric:tty2", message=cjk_message
        )
        assert not result.error
        lines = result.text.splitlines()
        assert len(lines) > 2  # lead + wrapped body lines
        for line in lines:
            assert visible_width(line) <= TABLE_WIDTH

    async def test_empty_message_is_a_caller_error(self, kai: CliContext) -> None:
        kai.talk.begin_connected(
            partner="eric", partner_tty="tty2", partner_key=_ERIC_KEY
        )
        result = await talk_commands.send_line(
            kai, to_key=_ERIC_KEY, display="eric:tty2", message=""
        )
        assert result.error
        assert "provide a message" in result.text.lower()

    async def test_refuses_when_not_connected(self, kai: CliContext) -> None:
        """send_line enforces its connected-partner precondition (PY-EH-1)."""
        # Idle: never became connected to anyone.
        result = await talk_commands.send_line(
            kai, to_key=_ERIC_KEY, display="eric:tty2", message="hi"
        )
        assert result.error
        assert "not connected" in result.text.lower()
        assert kai.talk.phase is TalkPhase.IDLE

    async def test_refuses_when_connected_to_a_different_partner(
        self, kai: CliContext
    ) -> None:
        kai.talk.begin_connected(
            partner="jo", partner_tty="tty9", partner_key="jo:johex001"
        )
        result = await talk_commands.send_line(
            kai, to_key=_ERIC_KEY, display="eric:tty2", message="hi"
        )
        assert result.error
        assert "not connected" in result.text.lower()
        assert kai.talk.partner_key == "jo:johex001"  # live partner untouched


class TestEndOrCancel:
    async def test_no_active_session(self, kai: CliContext) -> None:
        result = await talk_commands.end_or_cancel(kai)
        assert "no active talk session" in result.text.lower()
        assert kai.talk.phase is TalkPhase.IDLE

    async def test_end_while_inviting_returns_to_idle(self, kai: CliContext) -> None:
        kai.talk.begin_invite(partner="eric", partner_tty="tty2", partner_key=_ERIC_KEY)
        result = await talk_commands.end_or_cancel(kai)
        assert not result.error
        assert "eric" in result.text
        assert kai.talk.phase is TalkPhase.IDLE

    async def test_end_while_connected_returns_to_idle(self, kai: CliContext) -> None:
        kai.talk.begin_connected(
            partner="eric", partner_tty="tty2", partner_key=_ERIC_KEY
        )
        result = await talk_commands.end_or_cancel(kai)
        assert "talk session with eric ended" in result.text.lower()
        assert kai.talk.phase is TalkPhase.IDLE

    async def test_double_end_is_no_op(self, kai: CliContext) -> None:
        kai.talk.begin_connected(
            partner="eric", partner_tty="tty2", partner_key=_ERIC_KEY
        )
        await talk_commands.end_or_cancel(kai)
        second = await talk_commands.end_or_cancel(kai)
        assert "no active talk session" in second.text.lower()


class TestTalkDispatcher:
    async def test_offline_user_reports_not_online(self, kai: CliContext) -> None:
        result = await talk_commands.talk(kai, "@ghost", "")
        assert result.error
        assert "not online" in result.text.lower()
        assert kai.talk.phase is TalkPhase.IDLE

    async def test_idle_dispatch_starts_invite(self, kai: CliContext) -> None:
        await _register_eric(kai.relay)
        result = await talk_commands.talk(kai, "@eric:tty2", "")
        assert not result.error
        assert kai.talk.phase is TalkPhase.INVITING
        assert kai.talk.partner_key == _ERIC_KEY

    async def test_pending_invite_dispatch_accepts(self, kai: CliContext) -> None:
        await _register_eric(kai.relay)
        _feed_invite_from_eric(kai)
        result = await talk_commands.talk(kai, "@eric", "")
        assert not result.error
        assert kai.talk.phase is TalkPhase.CONNECTED
        assert kai.talk.pending_invites == {}

    async def test_connected_dispatch_sends_message(self, kai: CliContext) -> None:
        await _register_eric(kai.relay)
        kai.talk.begin_connected(
            partner="eric", partner_tty="tty2", partner_key=_ERIC_KEY
        )
        result = await talk_commands.talk(kai, "@eric:tty2", "ping")
        assert not result.error
        assert "sent to" in result.text.lower()
        assert kai.talk.phase is TalkPhase.CONNECTED
