"""Unit tests for talk — message formatting, state management, constants."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import biff.server.tools.talk as talk_mod
from biff.models import Message, UserSession
from biff.nats_relay import NatsRelay
from biff.server.tools._descriptions import _talk_description
from biff.server.tools._session import resolve_talk_target
from biff.server.tools.talk import (
    _NO_MESSAGES,
    format_agent_drain,
    format_talk_messages,
)
from biff.talk_state import TalkState
from biff.talk_types import AgentDrain, PendingInvite, TalkPhase

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.relay import Relay
    from biff.server.state import ServerState


class TestFormatTalkMessages:
    def test_single_message(self) -> None:
        msg = Message(
            from_user="kai",
            to_user="eric",
            body="check PR #42",
            timestamp=datetime(2026, 1, 15, 10, 30, 45, tzinfo=UTC),
        )
        result = format_talk_messages([msg])
        assert result == "[10:30:45] kai: check PR #42"

    def test_multiple_messages(self) -> None:
        msgs = [
            Message(
                from_user="kai",
                to_user="eric",
                body="first",
                timestamp=datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
            ),
            Message(
                from_user="eric",
                to_user="kai",
                body="second",
                timestamp=datetime(2026, 1, 15, 10, 0, 5, tzinfo=UTC),
            ),
        ]
        result = format_talk_messages(msgs)
        lines = result.split("\n")
        assert len(lines) == 2
        assert "kai: first" in lines[0]
        assert "eric: second" in lines[1]

    def test_empty_list(self) -> None:
        assert format_talk_messages([]) == ""


class TestFormatAgentDrain:
    """``talk_read`` names the inviter's session by its display tty.

    Same source as the ``[TALK]`` marker (``PendingInvite.accept_command``),
    so both surfaces render the reconciled ``talk @user:ttyN`` hint — the form
    ``/who`` shows and ``talk @user:ttyN`` resolves against — never the opaque
    session-key hex.
    """

    def test_accept_hint_uses_display_tty_not_key_hex(self) -> None:
        invite = PendingInvite(
            user="jfreeman",
            session_key="jfreeman:75abc665",
            tty="tty6",
            arrived=0.0,
        )
        drain = AgentDrain(messages=(), pending={"jfreeman": invite})

        rendered = format_agent_drain(drain)

        assert "talk @jfreeman:tty6" in rendered
        assert "75abc665" not in rendered

    def test_escapes_neutralized(self) -> None:
        """Remote body/sender can't inject terminal escapes (biff-lbj)."""
        msg = Message(
            from_user="ev\x1b[2Kil",
            to_user="kai",
            body="hi\x1b[2Jthere",
            timestamp=datetime(2026, 1, 15, 10, 30, 45, tzinfo=UTC),
        )
        result = format_talk_messages([msg])
        assert "\x1b[2J" not in result
        assert "\x1b[2K" not in result
        assert "hi[2Jthere" in result


class TestResolveTalkTarget:
    """resolve_talk_target maps a session address to a specific session key.

    Talk is session-scoped (DES-043): the address MUST name a session.
    """

    _SENDER = "kai:sender01"

    def test_bare_user_errors(self) -> None:
        """A bare @user has no unambiguous session — reject with a hint."""
        sessions = [UserSession(user="eric", tty="def456")]
        with pytest.raises(ValueError, match="specific session"):
            resolve_talk_target(sessions, "eric", None, sender_key=self._SENDER)

    def test_literal_tty_resolves(self) -> None:
        """When tty matches a literal session key, uses that key."""
        sessions = [UserSession(user="eric", tty="def456")]
        relay_key, display, target_repo = resolve_talk_target(
            sessions, "eric", "def456", sender_key=self._SENDER
        )
        assert relay_key == "eric:def456"
        assert display == "eric:def456"
        assert target_repo is None

    def test_tty_name_resolves_to_hex(self) -> None:
        """Friendly tty_name resolves to the session's actual hex key."""
        sessions = [UserSession(user="eric", tty="def456", tty_name="laptop")]
        relay_key, display, target_repo = resolve_talk_target(
            sessions, "eric", "laptop", sender_key=self._SENDER
        )
        assert relay_key == "eric:def456"
        assert display == "eric:laptop"
        assert target_repo is None

    def test_unresolved_tty_falls_back(self) -> None:
        """Unknown tty falls back to raw value (best-effort delivery)."""
        relay_key, display, target_repo = resolve_talk_target(
            [], "eric", "unknown", sender_key=self._SENDER
        )
        assert relay_key == "eric:unknown"
        assert display == "eric:unknown"
        assert target_repo is None

    def test_reaches_only_named_session(self) -> None:
        """Two sessions for one user — only the named tty is targeted."""
        sessions = [
            UserSession(user="eric", tty="aaa111", tty_name="laptop"),
            UserSession(user="eric", tty="bbb222", tty_name="desktop"),
        ]
        relay_key, _, _ = resolve_talk_target(
            sessions, "eric", "desktop", sender_key=self._SENDER
        )
        assert relay_key == "eric:bbb222"

    def test_self_talk_rejected(self) -> None:
        """Resolving to the sender's own session key is refused."""
        sessions = [UserSession(user="kai", tty="sender01", tty_name="here")]
        with pytest.raises(ValueError, match="your own session"):
            resolve_talk_target(sessions, "kai", "here", sender_key=self._SENDER)

    def test_cross_repo_sets_target_repo(self) -> None:
        """A session in a different repo yields its repo as target_repo."""
        sessions = [
            UserSession(user="eric", tty="ccc333", tty_name="peer", repo="other")
        ]
        relay_key, _, target_repo = resolve_talk_target(
            sessions, "eric", "peer", sender_key=self._SENDER, sender_repo="mine"
        )
        assert relay_key == "eric:ccc333"
        assert target_repo == "other"


class TestTalkNotificationToKey:
    """deliver's talk notification carries to_key for session-scoped targets."""

    def _relay_with_mock_nc(self) -> tuple[NatsRelay, AsyncMock]:
        relay = NatsRelay(
            url="nats://localhost", repo_name="myrepo", stream_prefix="biff-test"
        )
        nc = AsyncMock()
        nc.is_closed = False
        relay._nc = nc
        return relay, nc

    @staticmethod
    def _payload(nc: AsyncMock) -> dict[str, str]:
        nc.publish.assert_awaited_once()
        payload: dict[str, str] = json.loads(nc.publish.call_args[0][1])
        return payload

    async def test_targeted_sets_to_key(self) -> None:
        """A ``user:tty`` target puts to_key in the notification payload."""
        relay, nc = self._relay_with_mock_nc()
        msg = Message(
            from_user="kai", from_tty="tty1", to_user="eric:def456", body="hi"
        )
        await relay._publish_talk_notification("eric:def456", msg, "kai:abc123")
        assert self._payload(nc)["to_key"] == "eric:def456"

    async def test_broadcast_omits_to_key(self) -> None:
        """A bare ``user`` target (write/wall) has no to_key — broadcast."""
        relay, nc = self._relay_with_mock_nc()
        msg = Message(from_user="kai", from_tty="tty1", to_user="eric", body="hi")
        await relay._publish_talk_notification("eric", msg, "kai:abc123")
        assert "to_key" not in self._payload(nc)


class TestValidatedSenderKey:
    """NatsRelay._validated_sender_key drops invalid or mismatched keys."""

    def test_valid_key(self) -> None:
        result = NatsRelay._validated_sender_key("kai:abc123", "kai")
        assert result == "kai:abc123"

    def test_empty_key(self) -> None:
        assert NatsRelay._validated_sender_key("", "kai") == ""

    def test_no_colon(self) -> None:
        assert NatsRelay._validated_sender_key("kai", "kai") == ""

    def test_user_mismatch(self) -> None:
        assert NatsRelay._validated_sender_key("eric:abc123", "kai") == ""

    def test_empty_tty_part(self) -> None:
        assert NatsRelay._validated_sender_key("kai:", "kai") == ""

    def test_empty_user_part(self) -> None:
        assert NatsRelay._validated_sender_key(":abc123", "kai") == ""


class TestTalkEndResilience:
    """talk_end resets local state even when the best-effort publish fails."""

    async def test_publish_failure_still_resets_and_refreshes(self) -> None:
        """A wedged relay must not strand the session in a phantom talk state."""
        relay = MagicMock(spec=NatsRelay)
        relay.get_nc = AsyncMock(side_effect=TimeoutError("relay wedged"))
        talk = TalkState(
            relay=cast("Relay", relay),
            user="kai",
            tty="abc",
            session_key="kai:abc",
        )
        talk.begin_invite(partner="eric", partner_tty="def", partner_key="eric:def")
        state = MagicMock()
        state.talk = talk
        state.relay = relay
        refresh = AsyncMock()
        with patch.object(talk_mod, "refresh_talk", refresh):
            result = await talk_mod._do_talk_end(
                cast("FastMCP[ServerState]", MagicMock()),
                cast("ServerState", state),
            )
        assert talk.phase is TalkPhase.IDLE  # reset despite the publish failure
        refresh.assert_awaited_once()  # description refreshed regardless
        assert "time out" in result.lower()  # actionable transient message

    async def test_publish_failure_logs_at_info_not_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The best-effort publish failure stays off the WARNING stderr floor.

        The CLI raises the stderr handler to WARNING, so a WARNING here would
        dump a traceback into the interactive REPL; INFO keeps it in biff.log.
        """
        relay = MagicMock(spec=NatsRelay)
        relay.get_nc = AsyncMock(side_effect=TimeoutError("relay wedged"))
        talk = TalkState(
            relay=cast("Relay", relay),
            user="kai",
            tty="abc",
            session_key="kai:abc",
        )
        talk.begin_invite(partner="eric", partner_tty="def", partner_key="eric:def")
        state = MagicMock()
        state.talk = talk
        state.relay = relay
        with (
            patch.object(talk_mod, "refresh_talk", AsyncMock()),
            caplog.at_level(logging.INFO, logger="biff.server.tools.talk"),
        ):
            await talk_mod._do_talk_end(
                cast("FastMCP[ServerState]", MagicMock()),
                cast("ServerState", state),
            )
        records = [r for r in caplog.records if r.name == "biff.server.tools.talk"]
        assert records  # the failure was logged
        assert all(r.levelno == logging.INFO for r in records)


class TestDoTalk:
    """_do_talk: the invite hint carries ``@`` and the accept names display tty."""

    def _state(self, sessions: list[UserSession]) -> tuple[MagicMock, AsyncMock]:
        relay = MagicMock(spec=NatsRelay)
        nc = AsyncMock()
        relay.get_nc = AsyncMock(return_value=nc)
        relay.talk_notify_subject = MagicMock(return_value="biff.talk.jfreeman")
        relay.get_sessions_for_repos = AsyncMock(return_value=sessions)
        talk = TalkState(
            relay=cast("Relay", relay),
            user="kai",
            tty="kaihex01",
            session_key="kai:kaihex01",
            tty_name="tty1",
        )
        state = MagicMock()
        state.talk = talk
        state.relay = relay
        state.session_key = "kai:kaihex01"
        state.config.user = "kai"
        state.config.repo_name = "myrepo"
        state.visible_repos = frozenset({"myrepo"})
        return state, nc

    @staticmethod
    def _invite_frame() -> dict[str, str]:
        return {
            "type": "invite",
            "from": "jfreeman",
            "from_tty": "tty6",
            "from_key": "jfreeman:75abc665",
            "body": "wants to talk",
            "to_key": "kai:kaihex01",
        }

    async def test_invite_hint_carries_at_prefix(self) -> None:
        """A fresh invite body suggests a runnable ``talk @user:tty`` accept."""
        sessions = [
            UserSession(user="jfreeman", tty="75abc665", tty_name="tty6", repo="myrepo")
        ]
        state, nc = self._state(sessions)
        mcp = cast("FastMCP[ServerState]", MagicMock())
        with (
            patch.object(talk_mod, "update_current_session", AsyncMock()),
            patch.object(talk_mod, "refresh_talk", AsyncMock()),
            patch.object(talk_mod, "get_tty_name", return_value="tty1"),
        ):
            state_ = cast("ServerState", state)
            await talk_mod._do_talk(mcp, state_, "@jfreeman:tty6", "")
        body: str = json.loads(nc.publish.call_args[0][1])["body"]
        assert "talk @kai:tty1" in body

    async def test_accept_connected_hint_uses_display_tty_not_hex(self) -> None:
        """Accepting a pending invite names the partner by ``ttyN``, not the key hex."""
        sessions = [
            UserSession(user="jfreeman", tty="75abc665", tty_name="tty6", repo="myrepo")
        ]
        state, _ = self._state(sessions)
        state.talk.receive(self._invite_frame())
        state.talk.drain_idle()  # record the pending invite
        mcp = cast("FastMCP[ServerState]", MagicMock())
        with (
            patch.object(talk_mod, "update_current_session", AsyncMock()),
            patch.object(talk_mod, "refresh_talk", AsyncMock()),
        ):
            await talk_mod._do_talk(mcp, cast("ServerState", state), "@jfreeman", "")
        assert state.talk.partner_tty == "tty6"
        hint = _talk_description(state.talk)
        assert "talk @jfreeman:tty6" in hint
        assert "75abc665" not in hint

    async def test_invite_publish_failure_rolls_back_phase(self) -> None:
        """A transient send_invite failure must not strand the session INVITING."""
        sessions = [
            UserSession(user="jfreeman", tty="75abc665", tty_name="tty6", repo="myrepo")
        ]
        state, _ = self._state(sessions)
        state.relay.get_nc = AsyncMock(side_effect=TimeoutError("wedged"))
        mcp = cast("FastMCP[ServerState]", MagicMock())
        with (
            patch.object(talk_mod, "update_current_session", AsyncMock()),
            patch.object(talk_mod, "refresh_talk", AsyncMock()),
            patch.object(talk_mod, "get_tty_name", return_value="tty1"),
        ):
            result = await talk_mod._do_talk(
                mcp, cast("ServerState", state), "@jfreeman:tty6", ""
            )
        assert state.talk.phase is TalkPhase.IDLE  # rolled back, not stuck INVITING
        assert "not sent" in result.lower()

    async def test_accept_publish_failure_rolls_back_phase(self) -> None:
        """A transient send_accept failure must not strand the session CONNECTED."""
        sessions = [
            UserSession(user="jfreeman", tty="75abc665", tty_name="tty6", repo="myrepo")
        ]
        state, _ = self._state(sessions)
        state.talk.receive(self._invite_frame())
        state.talk.drain_idle()  # record the pending invite
        state.relay.get_nc = AsyncMock(side_effect=TimeoutError("wedged"))
        mcp = cast("FastMCP[ServerState]", MagicMock())
        with (
            patch.object(talk_mod, "update_current_session", AsyncMock()),
            patch.object(talk_mod, "refresh_talk", AsyncMock()),
        ):
            result = await talk_mod._do_talk(
                mcp, cast("ServerState", state), "@jfreeman", ""
            )
        assert state.talk.phase is TalkPhase.IDLE  # rolled back, not stuck CONNECTED
        assert "not sent" in result.lower()

    async def test_pending_invite_survives_failed_resolution(self) -> None:
        """An invite whose target fails to resolve stays acceptable later.

        Consuming before resolution would pop the invite and leave it
        unacceptable when the resolve then fails (offline/ambiguous tty).
        """
        state, _ = self._state([])  # jfreeman offline → resolution fails
        state.talk.receive(self._invite_frame())
        state.talk.drain_idle()  # record the pending invite
        assert "jfreeman" in state.talk.pending_invites
        mcp = cast("FastMCP[ServerState]", MagicMock())
        with (
            patch.object(talk_mod, "update_current_session", AsyncMock()),
            patch.object(talk_mod, "refresh_talk", AsyncMock()),
        ):
            result = await talk_mod._do_talk(
                mcp, cast("ServerState", state), "@jfreeman", ""
            )
        assert "not online" in result.lower()
        assert "jfreeman" in state.talk.pending_invites  # NOT consumed on failure

    async def test_successful_accept_consumes_invite(self) -> None:
        """A successful accept still consumes the pending invite (one-shot)."""
        sessions = [
            UserSession(user="jfreeman", tty="75abc665", tty_name="tty6", repo="myrepo")
        ]
        state, _ = self._state(sessions)
        state.talk.receive(self._invite_frame())
        state.talk.drain_idle()
        assert "jfreeman" in state.talk.pending_invites
        mcp = cast("FastMCP[ServerState]", MagicMock())
        with (
            patch.object(talk_mod, "update_current_session", AsyncMock()),
            patch.object(talk_mod, "refresh_talk", AsyncMock()),
        ):
            await talk_mod._do_talk(mcp, cast("ServerState", state), "@jfreeman", "")
        assert state.talk.pending_invites == {}  # consumed on success
        assert state.talk.phase is TalkPhase.CONNECTED

    async def test_connected_message_publish_failure_returns_transient(self) -> None:
        """A send_message failure while connected returns a message, never raises."""
        sessions = [
            UserSession(user="jfreeman", tty="75abc665", tty_name="tty6", repo="myrepo")
        ]
        state, _ = self._state(sessions)
        state.talk.begin_connected(
            partner="jfreeman", partner_tty="tty6", partner_key="jfreeman:75abc665"
        )
        state.relay.get_nc = AsyncMock(side_effect=TimeoutError("wedged"))
        mcp = cast("FastMCP[ServerState]", MagicMock())
        with (
            patch.object(talk_mod, "update_current_session", AsyncMock()),
            patch.object(talk_mod, "refresh_talk", AsyncMock()),
        ):
            result = await talk_mod._do_talk(
                mcp, cast("ServerState", state), "@jfreeman:tty6", "hello"
            )
        assert "not sent" in result.lower()  # transient, actionable
        assert state.talk.phase is TalkPhase.CONNECTED  # connection left intact


class TestDoTalkNoClobber:
    """Starting a new invite must never clobber a live talk (silent data loss).

    While CONNECTED to A, ``talk @B`` (B is a different online user with no
    pending invite) must refuse — leave the phase CONNECTED to A and send no
    frame — rather than overwrite the live connection with a fresh invite to B.
    """

    def _state(self, sessions: list[UserSession]) -> tuple[MagicMock, AsyncMock]:
        relay = MagicMock(spec=NatsRelay)
        nc = AsyncMock()
        relay.get_nc = AsyncMock(return_value=nc)
        relay.talk_notify_subject = MagicMock(return_value="biff.talk.eric")
        relay.get_sessions_for_repos = AsyncMock(return_value=sessions)
        talk = TalkState(
            relay=cast("Relay", relay),
            user="kai",
            tty="kaihex01",
            session_key="kai:kaihex01",
            tty_name="tty1",
        )
        state = MagicMock()
        state.talk = talk
        state.relay = relay
        state.session_key = "kai:kaihex01"
        state.config.user = "kai"
        state.config.repo_name = "myrepo"
        state.visible_repos = frozenset({"myrepo"})
        return state, nc

    @staticmethod
    def _sessions() -> list[UserSession]:
        return [
            UserSession(
                user="jfreeman", tty="75abc665", tty_name="tty6", repo="myrepo"
            ),
            UserSession(user="eric", tty="eric789", tty_name="tty9", repo="myrepo"),
        ]

    async def test_connected_to_a_talk_b_refuses_and_keeps_connection(self) -> None:
        """``talk @B`` while connected to A blocks, keeps A, sends nothing."""
        state, nc = self._state(self._sessions())
        state.talk.begin_connected(
            partner="jfreeman", partner_tty="tty6", partner_key="jfreeman:75abc665"
        )
        mcp = cast("FastMCP[ServerState]", MagicMock())
        with (
            patch.object(talk_mod, "update_current_session", AsyncMock()),
            patch.object(talk_mod, "refresh_talk", AsyncMock()),
        ):
            result = await talk_mod._do_talk(
                mcp, cast("ServerState", state), "@eric:tty9", "hi"
            )
        assert "already in a talk" in result.lower()
        assert "jfreeman:tty6" in result  # names the live partner
        assert state.talk.phase is TalkPhase.CONNECTED  # A not abandoned
        assert state.talk.partner_key == "jfreeman:75abc665"  # still A
        nc.publish.assert_not_called()  # no invite frame sent to B

    async def test_connected_to_a_talk_a_still_sends_message(self) -> None:
        """``talk @A`` while connected to A still routes to the send branch."""
        state, nc = self._state(self._sessions())
        state.talk.begin_connected(
            partner="jfreeman", partner_tty="tty6", partner_key="jfreeman:75abc665"
        )
        mcp = cast("FastMCP[ServerState]", MagicMock())
        with (
            patch.object(talk_mod, "update_current_session", AsyncMock()),
            patch.object(talk_mod, "refresh_talk", AsyncMock()),
        ):
            result = await talk_mod._do_talk(
                mcp, cast("ServerState", state), "@jfreeman:tty6", "hi"
            )
        assert "sent to" in result.lower()
        assert state.talk.phase is TalkPhase.CONNECTED
        payload: dict[str, str] = json.loads(nc.publish.call_args[0][1])
        assert payload["type"] == "message"
        assert payload["body"] == "hi"

    async def test_idle_talk_b_still_starts_invite(self) -> None:
        """``talk @B`` while idle still starts a fresh invite (unchanged)."""
        state, nc = self._state(self._sessions())
        mcp = cast("FastMCP[ServerState]", MagicMock())
        with (
            patch.object(talk_mod, "update_current_session", AsyncMock()),
            patch.object(talk_mod, "refresh_talk", AsyncMock()),
            patch.object(talk_mod, "get_tty_name", return_value="tty1"),
        ):
            result = await talk_mod._do_talk(
                mcp, cast("ServerState", state), "@eric:tty9", ""
            )
        assert "invite sent" in result.lower()
        assert state.talk.phase is TalkPhase.INVITING
        assert state.talk.partner_key == "eric:eric789"
        payload: dict[str, str] = json.loads(nc.publish.call_args[0][1])
        assert payload["type"] == "invite"

    async def test_inviting_b_talk_c_refuses(self) -> None:
        """``talk @C`` while an invite to B is outstanding blocks, sends nothing."""
        state, nc = self._state(
            [
                *self._sessions(),
                UserSession(user="priya", tty="pri111", tty_name="tty3", repo="myrepo"),
            ]
        )
        state.talk.begin_invite(
            partner="eric", partner_tty="tty9", partner_key="eric:eric789"
        )
        mcp = cast("FastMCP[ServerState]", MagicMock())
        with (
            patch.object(talk_mod, "update_current_session", AsyncMock()),
            patch.object(talk_mod, "refresh_talk", AsyncMock()),
        ):
            result = await talk_mod._do_talk(
                mcp, cast("ServerState", state), "@priya:tty3", ""
            )
        assert "already in a talk" in result.lower()
        assert state.talk.phase is TalkPhase.INVITING  # invite to B untouched
        assert state.talk.partner_key == "eric:eric789"
        nc.publish.assert_not_called()


class TestTalkDescriptionActions:
    """The idle-activity ``talk`` descriptions direct talk_read, not talk_end.

    ``talk_end`` returns "No active talk session" and clears nothing when idle
    with only pending invites or queued messages — ``talk_read`` is the action.
    """

    def _talk(self) -> TalkState:
        relay = MagicMock(spec=NatsRelay)
        return TalkState(
            relay=cast("Relay", relay),
            user="kai",
            tty="kaihex01",
            session_key="kai:kaihex01",
            tty_name="tty1",
        )

    def test_pending_invite_description_directs_talk_read(self) -> None:
        talk = self._talk()
        talk.receive(
            {
                "type": "invite",
                "from": "jfreeman",
                "from_tty": "tty6",
                "from_key": "jfreeman:75abc665",
                "body": "wants to talk",
                "to_key": "kai:kaihex01",
            }
        )
        talk.drain_idle()
        desc = _talk_description(talk)
        assert "[TALK]" in desc
        assert "talk_read" in desc
        assert "talk_end" not in desc

    def test_queued_message_description_directs_talk_read(self) -> None:
        talk = self._talk()
        talk.receive(
            {
                "type": "message",
                "from": "eric",
                "from_key": "eric:def67890",
                "body": "hi",
                "to_key": "kai:kaihex01",
            }
        )
        desc = _talk_description(talk)
        assert "[TALK]" in desc
        assert "talk_read" in desc
        assert "talk_end" not in desc


class TestConstants:
    def test_no_messages_sentinel(self) -> None:
        assert "talk" in _NO_MESSAGES.lower()
