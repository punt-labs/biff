"""Unit tests for talk — message formatting, state management, constants."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import biff.server.tools.talk as talk_mod
from biff.formatting import HEADER_PREFIX
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
from biff.talk_types import AgentDrain, PendingInvite, TalkNotification, TalkPhase

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

    def test_control_only_tty_and_body_collapse(self) -> None:
        """A control-only tty renders bare user; a control-only body renders nothing.

        Both fields are attacker-controlled (DES-046): a tty that is empty only
        after neutralisation must not leave a dangling ``user:`` label, and a
        body that neutralises to empty must produce no line at all (biff-7g7).
        """
        ctrl_tty = TalkNotification.from_payload(
            {
                "type": "message",
                "from": "eric",
                "from_tty": "\x00\x1b\x07",
                "from_key": "eric:def",
                "to_key": "kai:abc",
                "body": "hi",
            }
        )
        ctrl_body = TalkNotification.from_payload(
            {
                "type": "message",
                "from": "eric",
                "from_tty": "tty2",
                "from_key": "eric:def",
                "to_key": "kai:abc",
                "body": "\x00\x1b\x07",
            }
        )
        rendered = format_agent_drain(
            AgentDrain(messages=(ctrl_tty, ctrl_body), pending={})
        )
        # bare user, no dangling colon; body skipped; shared ▶ idiom prefix
        assert rendered == f"{HEADER_PREFIX}eric: hi"

    def test_whitespace_only_body_dropped(self) -> None:
        """A whitespace-only body renders nothing, matching the REPL render.

        Spaces survive terminal_safe (they are printable), so an all-whitespace
        body must be skipped here just as ``format_talk_line`` skips it — both
        surfaces agree, and the agent's context never shows a bare ``user:``.
        """
        blank = TalkNotification.from_payload(
            {
                "type": "message",
                "from": "eric",
                "from_tty": "tty2",
                "from_key": "eric:def",
                "to_key": "kai:abc",
                "body": "   ",
            }
        )
        real = TalkNotification.from_payload(
            {
                "type": "message",
                "from": "eric",
                "from_tty": "tty2",
                "from_key": "eric:def",
                "to_key": "kai:abc",
                "body": "hi",
            }
        )
        rendered = format_agent_drain(AgentDrain(messages=(blank, real), pending={}))
        assert rendered == f"{HEADER_PREFIX}eric:tty2: hi"

    def test_invite_uses_shared_arrow_not_phone(self) -> None:
        """The agent drain shares the ``▶`` idiom — no ``📞`` prefix (biff-7g7)."""
        invite = PendingInvite(
            user="jfreeman",
            session_key="jfreeman:75abc665",
            tty="tty6",
            arrived=0.0,
        )
        drain = AgentDrain(messages=(), pending={"jfreeman": invite})
        rendered = format_agent_drain(drain)
        assert rendered.startswith(HEADER_PREFIX)
        assert "📞" not in rendered

    def test_end_frame_renders_arrow_hangup_line(self) -> None:
        """An end frame renders via the shared ``format_talk_end`` idiom."""
        end = TalkNotification.from_payload(
            {
                "type": "end",
                "from": "eric",
                "from_tty": "tty2",
                "from_key": "eric:def",
                "to_key": "kai:abc",
            }
        )
        rendered = format_agent_drain(AgentDrain(messages=(end,), pending={}))
        assert rendered == f"{HEADER_PREFIX}eric:tty2 has ended the conversation."

    def test_stays_single_line_no_hang_indent(self) -> None:
        """Model-consumed output is single-line: one line per frame, no wrap indent."""
        long_body = "word " * 60  # would wrap across many terminal lines
        msg = TalkNotification.from_payload(
            {
                "type": "message",
                "from": "eric",
                "from_tty": "tty2",
                "from_key": "eric:def",
                "to_key": "kai:abc",
                "body": long_body,
            }
        )
        rendered = format_agent_drain(AgentDrain(messages=(msg,), pending={}))
        assert rendered.count("\n") == 0


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
        relay_key, display = resolve_talk_target(
            sessions, "eric", "def456", sender_key=self._SENDER
        )
        assert relay_key == "eric:def456"
        assert display == "eric:def456"

    def test_tty_name_resolves_to_hex(self) -> None:
        """Friendly tty_name resolves to the session's actual hex key."""
        sessions = [UserSession(user="eric", tty="def456", tty_name="laptop")]
        relay_key, display = resolve_talk_target(
            sessions, "eric", "laptop", sender_key=self._SENDER
        )
        assert relay_key == "eric:def456"
        assert display == "eric:laptop"

    def test_unresolved_tty_falls_back(self) -> None:
        """Unknown tty falls back to raw value (best-effort delivery)."""
        relay_key, display = resolve_talk_target(
            [], "eric", "unknown", sender_key=self._SENDER
        )
        assert relay_key == "eric:unknown"
        assert display == "eric:unknown"

    def test_reaches_only_named_session(self) -> None:
        """Two sessions for one user — only the named tty is targeted."""
        sessions = [
            UserSession(user="eric", tty="aaa111", tty_name="laptop"),
            UserSession(user="eric", tty="bbb222", tty_name="desktop"),
        ]
        relay_key, _ = resolve_talk_target(
            sessions, "eric", "desktop", sender_key=self._SENDER
        )
        assert relay_key == "eric:bbb222"

    def test_self_talk_rejected(self) -> None:
        """Resolving to the sender's own session key is refused."""
        sessions = [UserSession(user="kai", tty="sender01", tty_name="here")]
        with pytest.raises(ValueError, match="your own session"):
            resolve_talk_target(sessions, "kai", "here", sender_key=self._SENDER)

    def test_cross_repo_resolves_identity_not_repo(self) -> None:
        """A cross-repo peer resolves to its identity key — repo is not routed.

        Talk routes on (org, identity) (talk.tex ``subjectOf``); the peer's
        repository never enters the resolution.  The local repo still wins a
        tty_name collision (presence), but no target repo is returned.
        """
        sessions = [
            UserSession(user="eric", tty="ccc333", tty_name="peer", repo="other")
        ]
        relay_key, display = resolve_talk_target(
            sessions, "eric", "peer", sender_key=self._SENDER, sender_repo="mine"
        )
        assert relay_key == "eric:ccc333"
        assert display == "eric:peer"


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

    async def test_broadcast_publishes_no_wake(self) -> None:
        """A bare ``user`` target (write/wall) has no identity subject to wake.

        Talk frames route on ``(org, identity)`` (talk.tex ``subjectOf``); a
        bare-user broadcast names no single session, so no instant-wake frame
        is published.  The recipient still drains the durable inbox on its
        next poll tick.
        """
        relay, nc = self._relay_with_mock_nc()
        msg = Message(from_user="kai", from_tty="tty1", to_user="eric", body="hi")
        await relay._publish_talk_notification("eric", msg, "kai:abc123")
        nc.publish.assert_not_awaited()


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

    async def test_inviting_withdraw_failure_cites_the_ttl_sweep(self) -> None:
        """A wedged relay must not strand the session in a phantom talk state.

        The INVITING withdraw legitimately cites the TTL sweep: the invitee's
        pending invite is reaped by ``PENDING_INVITE_TTL`` (unlike a connected
        session), so the ~5-min timeout promise is accurate here.
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
        refresh = AsyncMock()
        with patch.object(talk_mod, "refresh_talk", refresh):
            result = await talk_mod._do_talk_end(
                cast("FastMCP[ServerState]", MagicMock()),
                cast("ServerState", state),
            )
        assert talk.phase is TalkPhase.IDLE  # reset despite the publish failure
        refresh.assert_awaited_once()  # description refreshed regardless
        assert "~5 min" in result  # the accurate pending-invite TTL promise

    async def test_connected_hangup_failure_names_real_outcome_not_ttl(self) -> None:
        """A failed connected hangup must not promise a TTL/auto-timeout recovery.

        ``PENDING_INVITE_TTL`` reaps pending invites, not a CONNECTED session, so
        a lost ``end`` frame leaves the peer connected — there is no ~5-min sweep
        for a live conversation.  The returned text must name that real outcome
        instead of the false reassurance the inviting-withdraw path can honestly
        make.
        """
        relay = MagicMock(spec=NatsRelay)
        relay.get_nc = AsyncMock(side_effect=TimeoutError("relay wedged"))
        talk = TalkState(
            relay=cast("Relay", relay),
            user="kai",
            tty="abc",
            session_key="kai:abc",
        )
        talk.begin_connected(partner="eric", partner_tty="def", partner_key="eric:def")
        state = MagicMock()
        state.talk = talk
        state.relay = relay
        with patch.object(talk_mod, "refresh_talk", AsyncMock()):
            result = await talk_mod._do_talk_end(
                cast("FastMCP[ServerState]", MagicMock()),
                cast("ServerState", state),
            )
        assert talk.phase is TalkPhase.IDLE  # local session ended regardless
        lowered = result.lower()
        assert "min" not in lowered  # no false ~5-min timeout promise
        assert "time out" not in lowered  # no false auto-timeout claim
        assert "eric" in result  # names the partner
        assert "may not" in lowered  # states the peer might not know

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

    async def test_accept_publish_failure_restores_pending_invite(self) -> None:
        """A failed accept publish must leave the invite acceptable for a retry.

        Consuming the invite before the publish and not restoring it on failure
        would strand the responder: the retry sends a fresh *outbound* invite
        instead of re-accepting.  The invite is restored so a retry re-accepts.
        """
        sessions = [
            UserSession(user="jfreeman", tty="75abc665", tty_name="tty6", repo="myrepo")
        ]
        state, _ = self._state(sessions)
        state.talk.receive(self._invite_frame())
        state.talk.drain_idle()
        state.relay.get_nc = AsyncMock(side_effect=TimeoutError("wedged"))
        mcp = cast("FastMCP[ServerState]", MagicMock())
        with (
            patch.object(talk_mod, "update_current_session", AsyncMock()),
            patch.object(talk_mod, "refresh_talk", AsyncMock()),
        ):
            await talk_mod._do_talk(mcp, cast("ServerState", state), "@jfreeman", "")
        assert "jfreeman" in state.talk.pending_invites  # restored, not lost

    async def test_retry_after_accept_failure_reaccepts_not_reinvites(self) -> None:
        """The restored invite lets a retry re-accept rather than re-invite."""
        sessions = [
            UserSession(user="jfreeman", tty="75abc665", tty_name="tty6", repo="myrepo")
        ]
        state, nc = self._state(sessions)
        state.talk.receive(self._invite_frame())
        state.talk.drain_idle()
        mcp = cast("FastMCP[ServerState]", MagicMock())
        with (
            patch.object(talk_mod, "update_current_session", AsyncMock()),
            patch.object(talk_mod, "refresh_talk", AsyncMock()),
        ):
            state.relay.get_nc = AsyncMock(side_effect=TimeoutError("wedged"))
            await talk_mod._do_talk(mcp, cast("ServerState", state), "@jfreeman", "")
            # Relay recovers; the retry must re-accept the still-pending invite.
            state.relay.get_nc = AsyncMock(return_value=nc)
            await talk_mod._do_talk(mcp, cast("ServerState", state), "@jfreeman", "")
        assert state.talk.phase is TalkPhase.CONNECTED
        assert state.talk.pending_invites == {}  # consumed on the successful retry
        sent_type = json.loads(nc.publish.call_args[0][1])["type"]
        assert sent_type == "accept"  # re-accept, not a fresh outbound invite

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

    async def test_invite_superseded_during_resolve_refuses(self) -> None:
        """A supersession during the resolve await must not accept the stale key.

        ``_do_talk`` peeks the pending invite, then awaits ``_resolve_target``.
        The always-on talk subscription can supersede the invite (a newer
        session of the same user) during that await.  Accepting the stale
        snapshot — or consuming whatever is now current — is a TOCTOU: the fix
        re-peeks after the await and refuses when the invite changed (CR-3).
        """
        sessions = [
            UserSession(user="jfreeman", tty="75abc665", tty_name="tty6", repo="myrepo")
        ]
        state, nc = self._state(sessions)
        state.talk.receive(self._invite_frame())
        state.talk.drain_idle()  # the invite we peek

        async def _supersede_during_resolve(
            _repos: object,
        ) -> list[UserSession]:
            # A newer invite from the same user lands while we resolve.
            state.talk.receive(
                {
                    "type": "invite",
                    "from": "jfreeman",
                    "from_tty": "tty9",
                    "from_key": "jfreeman:99newkey",
                    "body": "from my other session",
                    "to_key": "kai:kaihex01",
                }
            )
            state.talk.drain_idle()
            return sessions

        state.relay.get_sessions_for_repos = AsyncMock(
            side_effect=_supersede_during_resolve
        )
        mcp = cast("FastMCP[ServerState]", MagicMock())
        with (
            patch.object(talk_mod, "update_current_session", AsyncMock()),
            patch.object(talk_mod, "refresh_talk", AsyncMock()),
        ):
            result = await talk_mod._do_talk(
                mcp, cast("ServerState", state), "@jfreeman", ""
            )
        assert "changed" in result.lower()  # refused, not a stale-key connect
        assert state.talk.phase is TalkPhase.IDLE  # not connected to the stale key
        # The newer superseding invite is preserved, not consumed unchecked.
        assert state.talk.pending_invites["jfreeman"].session_key == "jfreeman:99newkey"
        nc.publish.assert_not_awaited()  # no accept sent to the stale key


class TestAgentAutoAcceptPublish:
    """talk_read/talk_listen publish the accept a mutual glare owes (F4).

    ``drain_for_agent`` connects the higher-key side but is pure state; the
    caller must emit the accept frame or the lower-key partner never connects
    (talk.tex ``MutualAutoAccept``).
    """

    @staticmethod
    def _state() -> tuple[MagicMock, AsyncMock]:
        relay = MagicMock(spec=NatsRelay)
        nc = AsyncMock()
        relay.get_nc = AsyncMock(return_value=nc)
        relay.talk_notify_subject = MagicMock(return_value="biff.talk.eric")
        talk = TalkState(
            relay=cast("Relay", relay),
            user="kai",  # 'kai' > 'eric' — the higher, auto-accepting side
            tty="kaihex01",
            session_key="kai:kaihex01",
            tty_name="tty1",
        )
        state = MagicMock()
        state.talk = talk
        state.relay = relay
        return state, nc

    async def test_publishes_accept_for_mutual_glare(self) -> None:
        state, nc = self._state()
        state.talk.begin_invite(
            partner="eric", partner_tty="tty2", partner_key="eric:def456"
        )
        state.talk.receive(
            {
                "type": "invite",
                "from": "eric",
                "from_tty": "tty2",
                "from_key": "eric:def456",
                "body": "talk?",
                "to_key": "kai:kaihex01",
            }
        )
        drain = state.talk.drain_for_agent()  # connects us; signals the accept
        assert drain.auto_accept is not None

        await talk_mod._publish_agent_auto_accept(cast("ServerState", state), drain)

        payload = json.loads(nc.publish.call_args[0][1])
        assert payload["type"] == "accept"
        assert payload["to_key"] == "eric:def456"  # to the invited session

    async def test_no_publish_without_auto_accept(self) -> None:
        state, nc = self._state()
        state.talk.receive(
            {
                "type": "invite",
                "from": "eric",
                "from_tty": "tty2",
                "from_key": "eric:def456",
                "body": "unsolicited",
                "to_key": "kai:kaihex01",
            }
        )
        drain = state.talk.drain_for_agent()  # plain invite — no glare
        assert drain.auto_accept is None

        await talk_mod._publish_agent_auto_accept(cast("ServerState", state), drain)

        nc.publish.assert_not_awaited()

    @staticmethod
    def _glare_drain(state: MagicMock) -> AgentDrain:
        """Drive a mutual glare and return the drain that owes an accept."""
        state.talk.begin_invite(
            partner="eric", partner_tty="tty2", partner_key="eric:def456"
        )
        state.talk.receive(
            {
                "type": "invite",
                "from": "eric",
                "from_tty": "tty2",
                "from_key": "eric:def456",
                "body": "talk?",
                "to_key": "kai:kaihex01",
            }
        )
        return cast("AgentDrain", state.talk.drain_for_agent())

    async def test_publish_retries_once_then_warns_on_failure(self) -> None:
        state, _ = self._state()
        state.relay.get_nc = AsyncMock(side_effect=TimeoutError("wedged"))
        drain = self._glare_drain(state)

        # Best-effort: a wedged relay must not raise out of talk_read, but the
        # persistent failure is reported so the agent (which cannot see the log)
        # learns the partner may not have connected.
        published = await talk_mod._publish_agent_auto_accept(
            cast("ServerState", state), drain
        )
        assert published is False
        assert state.relay.get_nc.await_count == 2  # retried once
        assert state.talk.phase is TalkPhase.CONNECTED  # still locally connected

        output = talk_mod._agent_drain_output(drain, accept_published=published)
        assert "eric:tty2" in output
        assert "may not have connected" in output

    async def test_publish_success_needs_no_retry_and_no_warning(self) -> None:
        state, nc = self._state()
        drain = self._glare_drain(state)

        published = await talk_mod._publish_agent_auto_accept(
            cast("ServerState", state), drain
        )
        assert published is True
        nc.publish.assert_awaited_once()  # one attempt, no retry

        output = talk_mod._agent_drain_output(drain, accept_published=published)
        assert "may not have connected" not in output


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

    @staticmethod
    def _record_invite(state: MagicMock, *, user: str, key: str, tty: str) -> None:
        """Record a pending invite from *user* by feeding and draining a frame."""
        state.talk.receive(
            {
                "type": "invite",
                "from": user,
                "from_tty": tty,
                "from_key": key,
                "body": "wants to talk",
                "to_key": "kai:kaihex01",
            }
        )
        state.talk.drain_idle()

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

    async def test_connected_to_a_accept_b_refuses_and_keeps_connection(self) -> None:
        """Accepting B's pending invite while connected to A blocks, keeps A."""
        state, nc = self._state(self._sessions())
        self._record_invite(state, user="jfreeman", key="jfreeman:75abc665", tty="tty6")
        state.talk.begin_connected(
            partner="eric", partner_tty="tty9", partner_key="eric:eric789"
        )
        mcp = cast("FastMCP[ServerState]", MagicMock())
        with (
            patch.object(talk_mod, "update_current_session", AsyncMock()),
            patch.object(talk_mod, "refresh_talk", AsyncMock()),
        ):
            result = await talk_mod._do_talk(
                mcp, cast("ServerState", state), "@jfreeman", "hi"
            )
        assert "already in a talk" in result.lower()
        assert "eric:tty9" in result  # names the live partner A
        assert state.talk.phase is TalkPhase.CONNECTED  # A not abandoned
        assert state.talk.partner_key == "eric:eric789"  # still A
        assert "jfreeman" in state.talk.pending_invites  # NOT consumed
        nc.publish.assert_not_called()  # no accept frame sent to B

    async def test_idle_accept_b_still_connects(self) -> None:
        """Accepting B's pending invite while idle still connects (unchanged)."""
        state, nc = self._state(self._sessions())
        self._record_invite(state, user="jfreeman", key="jfreeman:75abc665", tty="tty6")
        mcp = cast("FastMCP[ServerState]", MagicMock())
        with (
            patch.object(talk_mod, "update_current_session", AsyncMock()),
            patch.object(talk_mod, "refresh_talk", AsyncMock()),
        ):
            result = await talk_mod._do_talk(
                mcp, cast("ServerState", state), "@jfreeman", ""
            )
        assert "connected to" in result.lower()
        assert state.talk.phase is TalkPhase.CONNECTED
        assert state.talk.partner_key == "jfreeman:75abc665"
        assert state.talk.pending_invites == {}  # consumed on success
        assert json.loads(nc.publish.call_args[0][1])["type"] == "accept"

    async def test_inviting_b_accept_b_completes_mutual(self) -> None:
        """Accepting B's invite while INVITING B (glare) connects, not refuses."""
        state, _ = self._state(self._sessions())
        self._record_invite(state, user="jfreeman", key="jfreeman:75abc665", tty="tty6")
        state.talk.begin_invite(
            partner="jfreeman", partner_tty="tty6", partner_key="jfreeman:75abc665"
        )
        mcp = cast("FastMCP[ServerState]", MagicMock())
        with (
            patch.object(talk_mod, "update_current_session", AsyncMock()),
            patch.object(talk_mod, "refresh_talk", AsyncMock()),
        ):
            result = await talk_mod._do_talk(
                mcp, cast("ServerState", state), "@jfreeman", ""
            )
        assert "connected to" in result.lower()  # same partner → completes
        assert state.talk.phase is TalkPhase.CONNECTED
        assert state.talk.partner_key == "jfreeman:75abc665"
        assert state.talk.pending_invites == {}  # consumed on success

    async def test_inviting_b_accept_c_refuses(self) -> None:
        """Accepting C's invite while INVITING B blocks, keeps the B invite."""
        state, nc = self._state(self._sessions())
        self._record_invite(state, user="jfreeman", key="jfreeman:75abc665", tty="tty6")
        state.talk.begin_invite(
            partner="eric", partner_tty="tty9", partner_key="eric:eric789"
        )
        mcp = cast("FastMCP[ServerState]", MagicMock())
        with (
            patch.object(talk_mod, "update_current_session", AsyncMock()),
            patch.object(talk_mod, "refresh_talk", AsyncMock()),
        ):
            result = await talk_mod._do_talk(
                mcp, cast("ServerState", state), "@jfreeman", ""
            )
        assert "already in a talk" in result.lower()
        assert state.talk.phase is TalkPhase.INVITING  # invite to B untouched
        assert state.talk.partner_key == "eric:eric789"
        assert "jfreeman" in state.talk.pending_invites  # C's invite NOT consumed
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
