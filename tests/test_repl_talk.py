"""Tests for the REPL talk presentation layer (biff.__main__ formatters).

The talk *protocol* state machine lives in ``biff.talk_state`` and is
covered by ``tests/test_talk_state.py``.  These tests cover the CLI's
*rendering* of drained notifications — the ANSI banners, the timestamp
toggle, and terminal-escape neutralisation — which is the REPL front-end's
responsibility (talk.tex Drain* display side).
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

from biff.__main__ import (
    _enter_talk_phase,
    _format_idle_banners,
    _format_talk_lines,
    _handle_repl_talk,
    _print_talk_banner,
    _publish_auto_accept,
    _repl_talk,
    _run_talk_handshake,
    _talk_handshake,
    _withdraw_talk_invite,
)
from biff.models import UserSession
from biff.nats_relay import NatsRelay
from biff.repl_display import ReplDisplay
from biff.talk_state import TalkState
from biff.talk_types import PendingInvite, TalkNotification, TalkPhase

if TYPE_CHECKING:
    import pytest

    from biff.relay import Relay

OTHER_KEY = "eric:def67890"


def _notif(
    ntype: str,
    nfrom: str = "eric",
    nfrom_tty: str = "tty2",
    body: str = "",
    from_key: str = OTHER_KEY,
) -> TalkNotification:
    return TalkNotification(
        ntype=ntype,
        nfrom=nfrom,
        nfrom_tty=nfrom_tty,
        nfrom_key=from_key,
        nto="",
        nbody=body,
    )


# ---------------------------------------------------------------------------
# _format_talk_lines — connected-mode rendering
# ---------------------------------------------------------------------------


class TestFormatTalkLines:
    def test_empty(self) -> None:
        assert _format_talk_lines([]) == []

    def test_message_conversation_style(self) -> None:
        lines = _format_talk_lines([_notif("message", body="hello there")])
        assert len(lines) == 1
        assert "eric:tty2" in lines[0]
        assert "hello there" in lines[0]
        assert "\033[36m" in lines[0]  # cyan
        assert "📞" not in lines[0]

    def test_message_without_tty(self) -> None:
        lines = _format_talk_lines([_notif("message", nfrom_tty="", body="hi")])
        assert "eric ▶ hi" in lines[0]

    def test_empty_body_message_not_formatted(self) -> None:
        assert _format_talk_lines([_notif("message", body="")]) == []

    def test_end_renders_hangup(self) -> None:
        lines = _format_talk_lines([_notif("end")])
        assert len(lines) == 1
        assert "ended the conversation" in lines[0]
        assert "eric:tty2" in lines[0]

    def test_end_without_tty(self) -> None:
        lines = _format_talk_lines([_notif("end", nfrom_tty="")])
        assert "eric has ended" in lines[0]

    def test_multiple_messages(self) -> None:
        lines = _format_talk_lines(
            [_notif("message", body="first"), _notif("message", body="second")]
        )
        assert len(lines) == 2
        assert "first" in lines[0]
        assert "second" in lines[1]

    def test_mixed_message_and_end(self) -> None:
        lines = _format_talk_lines([_notif("message", body="bye"), _notif("end")])
        assert len(lines) == 2
        assert "bye" in lines[0]
        assert "ended the conversation" in lines[1]

    def test_no_timestamp_without_display(self) -> None:
        lines = _format_talk_lines([_notif("message", body="hi")])
        assert re.search(r"\[\d{2}:\d{2}\]", lines[0]) is None

    def test_no_timestamp_when_display_off(self) -> None:
        lines = _format_talk_lines([_notif("message", body="hi")], ReplDisplay())
        assert re.search(r"\[\d{2}:\d{2}\]", lines[0]) is None

    def test_timestamp_prefix_when_display_on(self) -> None:
        display = ReplDisplay()
        display.set_timestamps(on=True)
        lines = _format_talk_lines([_notif("message", body="hello")], display)
        assert re.search(r"\[\d{2}:\d{2}\] eric:tty2 ▶ hello", lines[0]) is not None

    def test_escape_injection_in_body_neutralized(self) -> None:
        lines = _format_talk_lines(
            [_notif("message", body="clear\x1b[2Jme\x1b]0;pwned\x07")]
        )
        assert "\x1b[2J" not in lines[0]
        assert "\x1b]0;" not in lines[0]
        assert "\x07" not in lines[0]
        assert "clear[2Jme]0;pwned" in lines[0]

    def test_escape_injection_in_sender_neutralized(self) -> None:
        lines = _format_talk_lines([_notif("message", nfrom="e\x1b[2Jvil", body="hi")])
        assert "\x1b[2J" not in lines[0]
        assert "e[2Jvil:tty2 ▶ hi" in lines[0]


# ---------------------------------------------------------------------------
# _format_idle_banners — idle-mode rendering
# ---------------------------------------------------------------------------


class TestFormatIdleBanners:
    def test_empty(self) -> None:
        assert _format_idle_banners([]) == []

    def test_invite_renders_phone_banner(self) -> None:
        lines = _format_idle_banners([_notif("invite", body="wants to talk")])
        assert len(lines) == 1
        assert "📞" in lines[0]
        assert "wants to talk" in lines[0]

    def test_accept_is_silent(self) -> None:
        assert _format_idle_banners([_notif("accept")]) == []

    def test_message_shows_sender_prefix(self) -> None:
        lines = _format_idle_banners([_notif("message", body="hi there")])
        assert len(lines) == 1
        assert "eric:tty2" in lines[0]
        assert "hi there" in lines[0]

    def test_end_without_body_renders_nothing(self) -> None:
        assert _format_idle_banners([_notif("end")]) == []

    def test_banner_stamped_when_display_on(self) -> None:
        display = ReplDisplay()
        display.set_timestamps(on=True)
        lines = _format_idle_banners([_notif("message", body="hi there")], display)
        assert re.search(r"\[\d{2}:\d{2}\] eric:tty2 ▶ hi there", lines[0]) is not None

    def test_banner_escape_injection_neutralized(self) -> None:
        lines = _format_idle_banners([_notif("message", body="hi\x1b[2Jthere")])
        assert "\x1b[2J" not in lines[0]
        assert "hi[2Jthere" in lines[0]


# ---------------------------------------------------------------------------
# _print_talk_banner — third-party banner during the accept wait
# ---------------------------------------------------------------------------


class TestPrintTalkBanner:
    def test_prints_banner_with_body(self, capsys: pytest.CaptureFixture[str]) -> None:
        _print_talk_banner(_notif("invite", nfrom="priya", body="wants to talk"))
        out = capsys.readouterr().out
        assert "priya" in out
        assert "wants to talk" in out
        assert "📞" in out

    def test_no_body_prints_nothing(self, capsys: pytest.CaptureFixture[str]) -> None:
        _print_talk_banner(_notif("invite", body=""))
        assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# _withdraw_talk_invite — best-effort withdraw log level (biff-9la)
# ---------------------------------------------------------------------------


class TestWithdrawTalkInviteResilience:
    """The best-effort withdraw failure stays off the WARNING stderr floor.

    The CLI raises the stderr handler to WARNING, so a WARNING here would dump
    a traceback into the interactive REPL; INFO keeps it in biff.log while the
    local state still resets and the invitee falls back to the TTL sweep.
    """

    async def test_publish_failure_logs_at_info_and_resets(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        relay = MagicMock(spec=NatsRelay)
        relay.get_nc = AsyncMock(side_effect=TimeoutError("relay wedged"))
        talk = TalkState(
            relay=cast("Relay", relay),
            user="kai",
            tty="abc",
            session_key="kai:abc",
        )
        talk.begin_invite(partner="eric", partner_tty="def", partner_key="eric:def")
        ctx = MagicMock()
        ctx.talk = talk
        ctx.relay = MagicMock()
        ctx.relay.get_session = AsyncMock(return_value=None)
        ctx.session_key = "kai:abc"

        with caplog.at_level(logging.INFO, logger="biff.__main__"):
            await _withdraw_talk_invite(ctx, "eric", "eric:def")

        assert talk.phase is TalkPhase.IDLE  # local state reset despite the failure
        records = [r for r in caplog.records if r.name == "biff.__main__"]
        assert records  # the failure was logged
        assert all(r.levelno == logging.INFO for r in records)


# ---------------------------------------------------------------------------
# _enter_talk_phase — refuse to clobber a live talk on the initiator path
# ---------------------------------------------------------------------------


class TestEnterTalkPhase:
    """Setting the handshake phase must never clobber a live talk.

    A ``talk @B`` (no pending invite from B) while CONNECTED to A — or while
    inviting a third party — must be refused, leaving the live partner intact
    and sending no frame, rather than overwriting it with a fresh invite.
    """

    def _ctx(self) -> MagicMock:
        relay = MagicMock(spec=NatsRelay)
        talk = TalkState(
            relay=cast("Relay", relay),
            user="kai",
            tty="kaihex01",
            session_key="kai:kaihex01",
            tty_name="tty1",
        )
        ctx = MagicMock()
        ctx.talk = talk
        return ctx

    def test_idle_initiator_begins_invite(self) -> None:
        ctx = self._ctx()
        proceed = _enter_talk_phase(
            ctx,
            user_target="eric",
            resolve_tty="tty2",
            target_key="eric:def456",
            pending=None,
        )
        assert proceed is True
        assert ctx.talk.phase is TalkPhase.INVITING
        assert ctx.talk.partner_key == "eric:def456"

    def test_pending_responder_begins_connected(self) -> None:
        ctx = self._ctx()
        pending = PendingInvite(
            user="eric", session_key="eric:def456", tty="tty2", arrived=0.0
        )
        proceed = _enter_talk_phase(
            ctx,
            user_target="eric",
            resolve_tty="tty2",
            target_key="eric:def456",
            pending=pending,
        )
        assert proceed is True
        assert ctx.talk.phase is TalkPhase.CONNECTED
        assert ctx.talk.partner_key == "eric:def456"

    def test_connected_to_a_initiator_to_b_refuses(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ctx = self._ctx()
        ctx.talk.begin_connected(
            partner="alice", partner_tty="tty7", partner_key="alice:aaa111"
        )
        proceed = _enter_talk_phase(
            ctx,
            user_target="eric",
            resolve_tty="tty2",
            target_key="eric:def456",
            pending=None,
        )
        assert proceed is False
        assert ctx.talk.phase is TalkPhase.CONNECTED  # A not abandoned
        assert ctx.talk.partner_key == "alice:aaa111"  # still A
        out = capsys.readouterr().out
        assert "already in a talk" in out.lower()
        assert "alice:tty7" in out  # names the live partner

    def test_inviting_b_initiator_to_c_refuses(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ctx = self._ctx()
        ctx.talk.begin_invite(
            partner="eric", partner_tty="tty2", partner_key="eric:def456"
        )
        proceed = _enter_talk_phase(
            ctx,
            user_target="priya",
            resolve_tty="tty3",
            target_key="priya:ccc333",
            pending=None,
        )
        assert proceed is False
        assert ctx.talk.phase is TalkPhase.INVITING  # invite to B untouched
        assert ctx.talk.partner_key == "eric:def456"
        assert "already in a talk" in capsys.readouterr().out.lower()

    def test_connected_to_a_accept_b_refuses(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Accepting B's invite while connected to A blocks, keeps A intact."""
        ctx = self._ctx()
        ctx.talk.begin_connected(
            partner="alice", partner_tty="tty7", partner_key="alice:aaa111"
        )
        pending = PendingInvite(
            user="eric", session_key="eric:def456", tty="tty2", arrived=0.0
        )
        proceed = _enter_talk_phase(
            ctx,
            user_target="eric",
            resolve_tty="tty2",
            target_key="eric:def456",
            pending=pending,
        )
        assert proceed is False
        assert ctx.talk.phase is TalkPhase.CONNECTED  # A not abandoned
        assert ctx.talk.partner_key == "alice:aaa111"  # still A
        out = capsys.readouterr().out
        assert "already in a talk" in out.lower()
        assert "alice:tty7" in out  # names the live partner A

    def test_inviting_b_accept_b_completes_mutual(self) -> None:
        """Accepting B's invite while INVITING B (glare) connects, not refuses."""
        ctx = self._ctx()
        ctx.talk.begin_invite(
            partner="eric", partner_tty="tty2", partner_key="eric:def456"
        )
        pending = PendingInvite(
            user="eric", session_key="eric:def456", tty="tty2", arrived=0.0
        )
        proceed = _enter_talk_phase(
            ctx,
            user_target="eric",
            resolve_tty="tty2",
            target_key="eric:def456",
            pending=pending,
        )
        assert proceed is True  # same partner → completes the handshake
        assert ctx.talk.phase is TalkPhase.CONNECTED
        assert ctx.talk.partner_key == "eric:def456"

    def test_inviting_b_accept_c_refuses(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Accepting C's invite while INVITING B blocks, keeps the B invite."""
        ctx = self._ctx()
        ctx.talk.begin_invite(
            partner="eric", partner_tty="tty2", partner_key="eric:def456"
        )
        pending = PendingInvite(
            user="priya", session_key="priya:ccc333", tty="tty3", arrived=0.0
        )
        proceed = _enter_talk_phase(
            ctx,
            user_target="priya",
            resolve_tty="tty3",
            target_key="priya:ccc333",
            pending=pending,
        )
        assert proceed is False
        assert ctx.talk.phase is TalkPhase.INVITING  # invite to B untouched
        assert ctx.talk.partner_key == "eric:def456"
        assert "already in a talk" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# _run_talk_handshake — invite/accept publish rollback (biff-9la, H)
# ---------------------------------------------------------------------------


class TestRunTalkHandshakeRollback:
    """A transient invite publish must not strand the REPL session.

    The plan is set and the phase advanced *before* the handshake publishes; a
    failed ``send_invite`` rolls both back so the session is not left INVITING
    with a phantom ``talking to …`` plan and no peer.
    """

    async def test_invite_publish_failure_rolls_back_phase_and_plan(self) -> None:
        relay = MagicMock(spec=NatsRelay)
        relay.get_nc = AsyncMock(side_effect=TimeoutError("wedged"))
        session = UserSession(
            user="kai", tty="kaihex01", tty_name="tty1", plan="idle", repo="myrepo"
        )
        relay.get_session = AsyncMock(return_value=session)
        relay.update_session = AsyncMock()
        talk = TalkState(
            relay=cast("Relay", relay),
            user="kai",
            tty="kaihex01",
            session_key="kai:kaihex01",
            tty_name="tty1",
        )
        talk.begin_invite(partner="eric", partner_tty="tty2", partner_key="eric:def456")
        ctx = MagicMock()
        ctx.talk = talk
        ctx.relay = relay
        ctx.session_key = "kai:kaihex01"
        ctx.user = "kai"
        ctx.tty_name = "tty1"

        proceed = await _run_talk_handshake(
            ctx,
            "eric",
            "eric:def456",
            "eric:tty2",
            ["@eric:tty2"],
            False,  # not responding — this is an outgoing invite
            asyncio.Queue(),
            asyncio.Event(),
            threading.Event(),
            target_repo=None,
        )

        assert proceed is False
        assert talk.phase is TalkPhase.IDLE  # phase rolled back, not stuck INVITING
        restored = relay.update_session.await_args_list[-1].args[0]
        assert restored.plan == "idle"  # plan restored to its prior value


# ---------------------------------------------------------------------------
# _repl_talk — connected-loop send resilience (F1)
# ---------------------------------------------------------------------------


class TestReplTalkSendResilience:
    """A wedged relay during a connected send must not crash the REPL.

    The server twin catches the publish trio and returns a "try again" line
    with the connection intact.  The REPL connected loop must do the same: a
    ``send_message`` that raises prints a notice and keeps the loop alive; a
    ``send_end`` that raises still returns to idle.  Left unguarded the error
    escapes ``asyncio.run``, dumps a traceback, and exits the process — losing
    the typed line.
    """

    @staticmethod
    def _connected_ctx() -> MagicMock:
        relay = MagicMock(spec=NatsRelay)
        # Every publish path (send_message/send_end) routes through get_nc.
        relay.get_nc = AsyncMock(side_effect=TimeoutError("relay wedged"))
        relay.talk_notify_subject = MagicMock(return_value="biff.t.talk.notify.eric")
        talk = TalkState(
            relay=cast("Relay", relay),
            user="kai",
            tty="kaihex01",
            session_key="kai:kaihex01",
            tty_name="tty1",
        )
        talk.begin_connected(
            partner="eric", partner_tty="tty2", partner_key="eric:def456"
        )
        ctx = MagicMock()
        ctx.talk = talk
        ctx.user = "kai"
        ctx.tty_name = "tty1"
        ctx.session_key = "kai:kaihex01"
        ctx.relay = MagicMock()
        ctx.relay.get_session = AsyncMock(return_value=None)
        return ctx

    async def _run(self, ctx: MagicMock, lines: list[str]) -> None:
        aqueue: asyncio.Queue[str | None] = asyncio.Queue()
        for line in lines:
            aqueue.put_nowait(line)
        await asyncio.wait_for(
            _repl_talk(
                ctx,
                "eric",
                "eric:tty2",
                aqueue,
                asyncio.Event(),
                threading.Event(),
                [""],
                "kai> ",
                ReplDisplay(),
                to_key="eric:def456",
                target_repo=None,
            ),
            timeout=5.0,
        )

    async def test_send_message_failure_keeps_loop_alive(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ctx = self._connected_ctx()
        # "hello" fails to publish; the loop must survive and process "end".
        await self._run(ctx, ["hello", "end"])
        out = capsys.readouterr().out
        assert "not sent" in out.lower()  # the failure surfaced as a notice
        assert ctx.talk.phase is TalkPhase.IDLE  # loop exited cleanly on end

    async def test_send_end_failure_still_returns_to_idle(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ctx = self._connected_ctx()
        # A wedged relay on the very hangup must still break to idle, no crash.
        await self._run(ctx, ["end"])
        assert ctx.talk.phase is TalkPhase.IDLE
        assert "not sent" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# _handle_repl_talk — responder accept-failure restores the invite (CR-2)
# ---------------------------------------------------------------------------


class TestReplAcceptRestoresPendingInvite:
    """A responder whose accept publish fails keeps the invite acceptable.

    The REPL twin of the MCP ``_accept_invite`` restore: consuming the invite
    before the accept and not restoring it would leave a retry sending a fresh
    outbound invite instead of re-accepting.
    """

    @staticmethod
    def _invite_frame(to_key: str) -> dict[str, str]:
        return {
            "type": "invite",
            "from": "jfreeman",
            "from_tty": "tty6",
            "from_key": "jfreeman:75abc665",
            "body": "wants to talk",
            "to_key": to_key,
        }

    async def test_accept_publish_failure_restores_invite(self) -> None:
        relay = MagicMock(spec=NatsRelay)
        relay.get_nc = AsyncMock(side_effect=TimeoutError("relay wedged"))
        relay.talk_notify_subject = MagicMock(return_value="biff.t.talk.notify.jf")
        relay.get_sessions_for_repos = AsyncMock(
            return_value=[
                UserSession(
                    user="jfreeman", tty="75abc665", tty_name="tty6", repo="myrepo"
                )
            ]
        )
        relay.get_session = AsyncMock(return_value=None)
        relay.update_session = AsyncMock()
        talk = TalkState(
            relay=cast("Relay", relay),
            user="kai",
            tty="kaihex01",
            session_key="kai:kaihex01",
            tty_name="tty1",
        )
        talk.receive(self._invite_frame("kai:kaihex01"))
        talk.drain_idle()  # record the pending invite
        assert "jfreeman" in talk.pending_invites
        ctx = MagicMock()
        ctx.talk = talk
        ctx.relay = relay
        ctx.session_key = "kai:kaihex01"
        ctx.user = "kai"
        ctx.tty_name = "tty1"
        ctx.config.repo_name = "myrepo"
        ctx.visible_repos = frozenset({"myrepo"})

        await _handle_repl_talk(
            ctx,
            ["@jfreeman"],
            asyncio.Queue(),
            asyncio.Event(),
            threading.Event(),
            [""],
            "kai> ",
            ReplDisplay(),
        )

        assert talk.phase is TalkPhase.IDLE  # not stranded CONNECTED
        assert "jfreeman" in talk.pending_invites  # restored for a retry


# ---------------------------------------------------------------------------
# _publish_auto_accept / mutual-glare auto-accept publish (F2)
# ---------------------------------------------------------------------------


class TestPublishAutoAccept:
    """The higher-key auto-accept must actually reach the partner (F2).

    The lower-key side connects ONLY on receiving this accept (talk.tex
    MutualAutoAccept — no symmetric fallback), so a dropped accept strands it.
    The publish retries once, and the handshake warns the user when both fail.
    """

    @staticmethod
    def _talk(*, get_nc: AsyncMock) -> TalkState:
        relay = MagicMock(spec=NatsRelay)
        relay.get_nc = get_nc
        relay.talk_notify_subject = MagicMock(return_value="biff.t.talk.notify.eric")
        return TalkState(
            relay=cast("Relay", relay),
            user="kai",
            tty="kaihex01",
            session_key="kai:kaihex01",
            tty_name="tty1",
        )

    async def test_retries_once_then_gives_up_on_persistent_failure(self) -> None:
        get_nc = AsyncMock(side_effect=TimeoutError("wedged"))
        talk = self._talk(get_nc=get_nc)
        ctx = MagicMock()
        ctx.talk = talk

        ok = await _publish_auto_accept(ctx, "eric", "eric:def456", target_repo=None)

        assert ok is False
        assert get_nc.await_count == 2  # published once, retried once

    async def test_succeeds_without_retry_when_publish_works(self) -> None:
        get_nc = AsyncMock(return_value=AsyncMock())
        talk = self._talk(get_nc=get_nc)
        ctx = MagicMock()
        ctx.talk = talk

        ok = await _publish_auto_accept(ctx, "eric", "eric:def456", target_repo=None)

        assert ok is True
        assert get_nc.await_count == 1  # no retry on success

    async def test_handshake_warns_when_auto_accept_never_reaches_partner(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # send_invite succeeds (get_nc call 1), both accept attempts fail (2, 3).
        nc = AsyncMock()
        get_nc = AsyncMock(
            side_effect=[nc, TimeoutError("wedged"), TimeoutError("wedged")]
        )
        talk = self._talk(get_nc=get_nc)
        # We are the higher key ('kai' > 'eric') — the auto-accepting side.
        talk.begin_invite(partner="eric", partner_tty="tty2", partner_key="eric:def456")
        talk.receive(
            {
                "type": "invite",
                "from": "eric",
                "from_tty": "tty2",
                "from_key": "eric:def456",
                "body": "talk?",
                "to_key": "kai:kaihex01",
            }
        )
        ctx = MagicMock()
        ctx.talk = talk
        ctx.user = "kai"
        ctx.tty_name = "tty1"
        notify = asyncio.Event()
        notify.set()  # wake the accept poll immediately

        proceed = await _talk_handshake(
            ctx,
            "eric",
            "eric:def456",
            "eric:tty2",
            ["@eric"],
            False,  # initiating — glare completes via MutualAutoAccept
            asyncio.Queue(),
            notify,
            threading.Event(),
            target_repo=None,
        )

        out = capsys.readouterr().out.lower()
        assert proceed is True  # we are connected locally; proceed to the loop
        assert "may not have" in out  # user warned the partner might be stranded
        assert get_nc.await_count == 3  # invite + two accept attempts (retry)
