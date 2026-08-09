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
import queue
import re
import threading
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import biff.commands.talk as talk_commands
from biff.__main__ import (
    _NO_INPUT,
    _clear_talk_plan,
    _format_idle_banners,
    _format_talk_lines,
    _handle_repl_talk,
    _initiate_talk,
    _print_talk_banner,
    _repl_talk,
    _ReplTalkSubscription,
    _set_talk_plan,
    _talk_converse,
    _talk_loop,
    _TalkSubscription,
    _wait_for_talk_accept,
    _withdraw_talk_invite,
)
from biff.models import UserSession
from biff.nats_relay import NatsRelay
from biff.repl_display import ReplDisplay
from biff.talk_state import TalkState
from biff.talk_types import TalkNotification, TalkPhase

if TYPE_CHECKING:
    import pytest

    from biff.relay import Relay

OTHER_KEY = "eric:def67890"

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    """Drop SGR colour codes so plain layout can be asserted."""
    return _ANSI.sub("", s)


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
        assert "▶  eric:tty2  hello there" in lines[0]
        assert "\033[36m" in lines[0]  # cyan
        assert "📞" not in lines[0]

    def test_message_without_tty(self) -> None:
        lines = _format_talk_lines([_notif("message", nfrom_tty="", body="hi")])
        assert "▶  eric  hi" in lines[0]

    def test_control_only_body_renders_fallback(self) -> None:
        # A body empty only after neutralisation still arrived — the recipient
        # sees an explanatory fallback line, not silence (biff-2sw round 6).
        lines = _format_talk_lines([_notif("message", body="\x00\x1b\x07")])
        assert len(lines) == 1
        assert "no printable text" in lines[0]

    def test_long_message_wraps_with_aligned_continuation(self) -> None:
        body = "word " * 30  # far wider than the 80-column table
        lines = _format_talk_lines([_notif("message", body=body.strip())])
        assert len(lines) > 1
        # First line carries the ▶ prefix and the sender; continuations do not.
        assert "▶  eric:tty2  " in lines[0]
        assert "▶" not in _strip_ansi(lines[1])
        # Continuation aligns under the body, not at column 0.
        assert _strip_ansi(lines[1]).startswith("   ")

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
        assert re.search(r"▶  \[\d{2}:\d{2}\] eric:tty2  hello", lines[0]) is not None

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
        assert "e[2Jvil:tty2  hi" in lines[0]


# ---------------------------------------------------------------------------
# _format_idle_banners — idle-mode rendering
# ---------------------------------------------------------------------------


class TestFormatIdleBanners:
    def test_empty(self) -> None:
        assert _format_idle_banners([]) == []

    def test_invite_renders_talk_idiom(self) -> None:
        lines = _format_idle_banners([_notif("invite", body="wants to talk")])
        assert len(lines) == 1
        assert "▶  eric:tty2  wants to talk" in lines[0]
        assert "📞" not in lines[0]
        assert "\033[1;33m" in lines[0]  # yellow

    def test_accept_is_silent(self) -> None:
        assert _format_idle_banners([_notif("accept")]) == []

    def test_message_shows_sender_prefix(self) -> None:
        lines = _format_idle_banners([_notif("message", body="hi there")])
        assert len(lines) == 1
        assert "eric:tty2" in lines[0]
        assert "hi there" in lines[0]

    def test_end_without_body_renders_nothing(self) -> None:
        assert _format_idle_banners([_notif("end")]) == []

    def test_control_only_body_renders_fallback(self) -> None:
        # A body empty only after neutralisation still arrived — the recipient
        # sees an explanatory fallback line, not silence (biff-2sw round 6).
        lines = _format_idle_banners([_notif("message", body="\x00\x1b\x07")])
        assert len(lines) == 1
        assert "no printable text" in lines[0]

    def test_banner_stamped_when_display_on(self) -> None:
        display = ReplDisplay()
        display.set_timestamps(on=True)
        lines = _format_idle_banners([_notif("message", body="hi there")], display)
        pattern = r"▶  \[\d{2}:\d{2}\] eric:tty2  hi there"
        assert re.search(pattern, lines[0]) is not None

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
        assert "▶  priya:tty2  wants to talk" in out
        assert "📞" not in out

    def test_no_body_prints_nothing(self, capsys: pytest.CaptureFixture[str]) -> None:
        _print_talk_banner(_notif("invite", body=""))
        assert capsys.readouterr().out == ""

    def test_control_only_body_prints_fallback(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A control-only body still arrived — it must print an explanatory
        # banner, not silence (biff-2sw round 6).
        _print_talk_banner(_notif("invite", body="\x00\x1b\x07"))
        out = capsys.readouterr().out
        assert "no printable text" in out


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
        relay.get_session = AsyncMock(return_value=None)
        talk = TalkState(
            relay=cast("Relay", relay),
            user="kai",
            tty="abc",
            session_key="kai:abc",
        )
        talk.begin_invite(partner="eric", partner_tty="def", partner_key="eric:def")
        ctx = MagicMock()
        ctx.talk = talk
        ctx.relay = relay
        ctx.session_key = "kai:abc"

        # The withdraw delegates to the shared end_or_cancel kernel, so the
        # best-effort publish failure is logged under biff.commands.talk.
        with caplog.at_level(logging.INFO, logger="biff.commands.talk"):
            await _withdraw_talk_invite(ctx)

        assert talk.phase is TalkPhase.IDLE  # local state reset despite the failure
        records = [r for r in caplog.records if r.name == "biff.commands.talk"]
        assert records  # the failure was logged
        assert all(r.levelno == logging.INFO for r in records)


# ---------------------------------------------------------------------------
# _handle_repl_talk initiator — invite publish rollback (biff-9la, H)
# ---------------------------------------------------------------------------


class TestReplInviteRollback:
    """A transient invite publish must not strand the REPL session.

    The initiator delegates the invite to the shared ``invite`` kernel, which
    resets the phase on a failed ``send_invite``.  The REPL sets the ``talking
    to …`` plan only *after* a successful publish, so a failed invite leaves the
    phase idle and never writes a phantom ``talking to …`` presence.
    """

    async def test_invite_publish_failure_leaves_phase_idle_no_plan(self) -> None:
        relay = MagicMock(spec=NatsRelay)
        relay.get_nc = AsyncMock(side_effect=TimeoutError("wedged"))
        relay.get_sessions_for_repos = AsyncMock(
            return_value=[
                UserSession(user="eric", tty="def456", tty_name="tty2", repo="myrepo")
            ]
        )
        relay.get_session = AsyncMock(
            return_value=UserSession(
                user="kai", tty="kaihex01", tty_name="tty1", plan="idle", repo="myrepo"
            )
        )
        relay.update_session = AsyncMock()
        talk = TalkState(
            relay=cast("Relay", relay),
            user="kai",
            tty="kaihex01",
            session_key="kai:kaihex01",
            tty_name="tty1",
        )
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
            ["@eric:tty2"],
            asyncio.Queue(),
            asyncio.Event(),
            threading.Event(),
            [""],
            "kai> ",
            ReplDisplay(),
        )

        assert talk.phase is TalkPhase.IDLE  # kernel rolled the invite back
        # The plan is written only after a successful publish — a failed invite
        # must never leave a phantom ``talking to …`` presence.
        planned = [c.args[0].plan for c in relay.update_session.await_args_list]
        assert all("talking to" not in p for p in planned)


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
                "eric:tty2",
                aqueue,
                asyncio.Event(),
                threading.Event(),
                [""],
                "kai> ",
                ReplDisplay(),
                to_key="eric:def456",
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
# publish_auto_accept / mutual-glare auto-accept publish (F2)
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

        ok = await talk_commands.publish_auto_accept(ctx, to_key="eric:def456")

        assert ok is False
        assert get_nc.await_count == 2  # published once, retried once

    async def test_succeeds_without_retry_when_publish_works(self) -> None:
        get_nc = AsyncMock(return_value=AsyncMock())
        talk = self._talk(get_nc=get_nc)
        ctx = MagicMock()
        ctx.talk = talk

        ok = await talk_commands.publish_auto_accept(ctx, to_key="eric:def456")

        assert ok is True
        assert get_nc.await_count == 1  # no retry on success

    async def test_initiator_warns_when_auto_accept_never_reaches_partner(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The REPL initiator warns when the owed auto-accept never publishes.

        Drives ``_initiate_talk`` end to end: the invite publishes (get_nc call
        1), the queued mutual-glare invite makes ``poll_accept`` auto-accept, and
        both owed-accept attempts fail (calls 2, 3) — so the user is warned the
        partner may be stranded, and the flow still proceeds to the loop.
        """
        # send_invite succeeds (get_nc call 1), both accept attempts fail (2, 3).
        nc = AsyncMock()
        get_nc = AsyncMock(
            side_effect=[nc, TimeoutError("wedged"), TimeoutError("wedged")]
        )
        talk = self._talk(get_nc=get_nc)
        # A mutual-glare invite from the partner is already queued; we are the
        # higher key ('kai' > 'eric') — the auto-accepting side.
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
        # _set_talk_plan reads/writes the session via ctx.relay (a distinct path
        # from talk's publish relay); a None session makes it a clean no-op.
        ctx.relay = MagicMock(spec=NatsRelay)
        ctx.relay.get_session = AsyncMock(return_value=None)
        ctx.session_key = "kai:kaihex01"
        ctx.config.user = "kai"
        notify = asyncio.Event()
        notify.set()  # wake the accept poll immediately

        proceed = await _initiate_talk(
            ctx,
            user_target="eric",
            target_key="eric:def456",
            display="eric:tty2",
            resolve_tty="tty2",
            opening="",
            aqueue=asyncio.Queue(),
            notify_event=notify,
            prompt_gate=threading.Event(),
        )

        out = capsys.readouterr().out.lower()
        assert proceed is True  # we are connected locally; proceed to the loop
        assert "may not have" in out  # user warned the partner might be stranded
        assert get_nc.await_count == 3  # invite + two accept attempts (retry)


class TestReplTalkSubscriptionResilience:
    """A failed re-subscribe must not crash the REPL — it retries next tick.

    The re-subscribe runs on the idle tick in exactly the flaky window a client
    replacement opens: ``get_nc``/``subscribe`` can raise ``NatsError``,
    ``TimeoutError``, ``OSError``, or a base ``nats.errors.Error``.  An
    unguarded raise dumps a traceback and exits the REPL, killing the retry
    loop that was meant to self-heal (biff-9la).
    """

    @staticmethod
    def _wedged_ctx() -> MagicMock:
        relay = MagicMock(spec=NatsRelay)
        relay.get_nc = AsyncMock(side_effect=TimeoutError("dial in progress"))
        relay.connection_generation = 1
        relay.talk_notify_subject = MagicMock(return_value="biff.talk.kai")
        ctx = MagicMock()
        ctx.relay = relay
        ctx.user = "kai"
        return ctx

    async def test_reconcile_survives_failed_resubscribe(self) -> None:
        ctx = self._wedged_ctx()
        sub = _ReplTalkSubscription(ctx, asyncio.Event())

        await sub.reconcile()  # must not raise

        assert sub._handle is None  # nothing bound on the dead client
        assert sub._generation == 0  # generation unchanged — binds only on success

    async def test_reconcile_reattempts_after_failure(self) -> None:
        ctx = self._wedged_ctx()
        sub = _ReplTalkSubscription(ctx, asyncio.Event())

        await sub.reconcile()
        await sub.reconcile()  # a second tick re-attempts (self-heals)

        assert ctx.relay.get_nc.await_count == 2

    async def test_resubscribe_failure_warns_once_then_recovers(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        ctx = self._wedged_ctx()
        sub = _ReplTalkSubscription(ctx, asyncio.Event())

        with caplog.at_level(logging.DEBUG, logger="biff.__main__"):
            await sub.reconcile()  # onset — one WARNING
            await sub.reconcile()  # retry — DEBUG only

            warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
            assert len(warnings) == 1

            # NATS recovers: get_nc + subscribe now succeed.
            ctx.relay.get_nc = AsyncMock(return_value=AsyncMock())
            ctx.relay.connection_generation = 2
            await sub.reconcile()

        assert sub._handle is not None
        assert sub._generation == 2
        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(infos) == 1  # one recovery line

    async def test_startup_establish_survives_failure(self) -> None:
        """The startup ``establish`` is crash-safe via the same guard."""
        ctx = self._wedged_ctx()
        sub = _ReplTalkSubscription(ctx, asyncio.Event())

        await sub.establish()  # startup call — must not raise

        assert sub._handle is None


class TestModalTalkReconcile:
    """A client swap during a modal talk path re-binds the talk SUB.

    The idle-tick reconcile fires only from the REPL input loop.  During a live
    conversation ``_repl_talk`` runs instead, and the invite-accept wait
    ``_wait_for_talk_accept`` blocks outside the idle path.  A wedge teardown
    that swaps the NATS client mid-talk orphans the SUB on the dead client, so
    incoming partner messages stop silently.  Both modal loops must reconcile
    on each poll tick so the SUB re-binds regardless of REPL mode.
    """

    @staticmethod
    def _ctx_relay_nc() -> tuple[MagicMock, MagicMock, MagicMock]:
        nc = MagicMock()
        nc.subscribe = AsyncMock(side_effect=[AsyncMock(), AsyncMock()])
        nc.publish = AsyncMock()
        relay = MagicMock(spec=NatsRelay)
        relay.get_nc = AsyncMock(return_value=nc)
        relay.connection_generation = 1
        relay.talk_notify_subject = MagicMock(return_value="biff.talk.kai")
        relay.get_session = AsyncMock(return_value=None)
        relay.update_session = AsyncMock()
        talk = TalkState(
            relay=cast("Relay", relay),
            user="kai",
            tty="kaihex01",
            session_key="kai:kaihex01",
            tty_name="tty1",
        )
        ctx = MagicMock()
        ctx.talk = talk
        ctx.user = "kai"
        ctx.tty_name = "tty1"
        ctx.session_key = "kai:kaihex01"
        ctx.relay = relay
        return ctx, relay, nc

    @staticmethod
    async def _bound_sub(ctx: MagicMock) -> _ReplTalkSubscription:
        sub = _ReplTalkSubscription(ctx, asyncio.Event())
        await sub.establish()  # binds generation 1, first subscribe
        return sub

    async def _drive_repl_talk(
        self, ctx: MagicMock, sub: _ReplTalkSubscription
    ) -> None:
        ctx.talk.begin_connected(
            partner="eric", partner_tty="tty2", partner_key="eric:def456"
        )
        aqueue: asyncio.Queue[str | None] = asyncio.Queue()
        task = asyncio.create_task(
            _repl_talk(
                ctx,
                "eric:tty2",
                aqueue,
                asyncio.Event(),
                threading.Event(),
                [""],
                "kai> ",
                ReplDisplay(),
                to_key="eric:def456",
                talk_sub=sub,
            )
        )
        # ``_repl_talk`` sets the notify event up front, so the first tick is an
        # idle poll where the reconcile fires; then ``end`` breaks the loop.
        await asyncio.sleep(0.05)
        aqueue.put_nowait("end")
        await asyncio.wait_for(task, timeout=5.0)

    async def _drive_accept_wait(
        self, ctx: MagicMock, sub: _ReplTalkSubscription
    ) -> None:
        ctx.talk.begin_invite(
            partner="eric", partner_tty="tty2", partner_key="eric:def456"
        )
        aqueue: asyncio.Queue[str | None] = asyncio.Queue()
        notify_event = asyncio.Event()
        notify_event.set()  # force an immediate idle tick
        task = asyncio.create_task(
            _wait_for_talk_accept(
                ctx, aqueue, notify_event, threading.Event(), talk_sub=sub
            )
        )
        await asyncio.sleep(0.05)
        aqueue.put_nowait("end")
        await asyncio.wait_for(task, timeout=5.0)

    async def test_repl_talk_reconciles_on_client_swap(self) -> None:
        ctx, relay, nc = self._ctx_relay_nc()
        sub = await self._bound_sub(ctx)
        relay.connection_generation = 2  # a mid-talk client replacement

        await self._drive_repl_talk(ctx, sub)

        assert nc.subscribe.await_count == 2  # establish + reconcile re-bind
        assert sub._generation == 2  # bound to the new client's generation

    async def test_repl_talk_reconcile_noop_when_generation_unchanged(self) -> None:
        ctx, _relay, nc = self._ctx_relay_nc()
        sub = await self._bound_sub(ctx)  # generation stays 1

        await self._drive_repl_talk(ctx, sub)

        assert nc.subscribe.await_count == 1  # no re-subscribe — a no-op
        assert sub._generation == 1

    async def test_accept_wait_reconciles_on_client_swap(self) -> None:
        ctx, relay, nc = self._ctx_relay_nc()
        sub = await self._bound_sub(ctx)
        relay.connection_generation = 2  # a client replacement while waiting

        await self._drive_accept_wait(ctx, sub)

        assert nc.subscribe.await_count == 2  # establish + reconcile re-bind
        assert sub._generation == 2

    async def test_accept_wait_reconcile_noop_when_generation_unchanged(self) -> None:
        ctx, _relay, nc = self._ctx_relay_nc()
        sub = await self._bound_sub(ctx)  # generation stays 1

        await self._drive_accept_wait(ctx, sub)

        assert nc.subscribe.await_count == 1  # no re-subscribe — a no-op
        assert sub._generation == 1


class TestStandaloneTalkReconcile:
    """A client swap during a ``biff talk`` session re-binds the notify SUB.

    The standalone command runs ``_talk_converse`` — not the REPL modal loops —
    so it needs its own generation-tracked re-subscribe.  A wedge teardown that
    swaps the NATS client mid-session orphans the notify SUB on the dead client
    and silently stops incoming partner messages (sends still redial); the loop
    must reconcile on each idle tick to re-bind it (nats-relay.tex talkSubGen).
    """

    @staticmethod
    def _relay_nc() -> tuple[MagicMock, MagicMock]:
        nc = MagicMock()
        nc.subscribe = AsyncMock(side_effect=[AsyncMock(), AsyncMock()])
        relay = MagicMock(spec=NatsRelay)
        relay.get_nc = AsyncMock(return_value=nc)
        relay.connection_generation = 1
        relay.talk_notify_subject = MagicMock(return_value="biff.talk.kai")
        relay.deliver = AsyncMock()
        return relay, nc

    @staticmethod
    async def _bound_sub(relay: MagicMock) -> _TalkSubscription:
        sub = _TalkSubscription(relay, "kai:kaihex01", asyncio.Event())
        await sub.establish()  # binds generation 1, first subscribe
        return sub

    async def _drive_converse(
        self,
        relay: MagicMock,
        sub: _TalkSubscription,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # One idle tick (where the reconcile fires), then EOF breaks the loop.
        monkeypatch.setattr(
            "biff.__main__._wait_for_input_or_notify",
            AsyncMock(side_effect=[_NO_INPUT, None]),
        )
        monkeypatch.setattr("biff.__main__._talk_fetch_and_print", AsyncMock())
        await asyncio.wait_for(
            _talk_converse(
                relay,
                sub,
                asyncio.Queue(),
                asyncio.Event(),
                "kai:kaihex01",
                "kai",
                "eric",
            ),
            timeout=5.0,
        )

    async def test_reconciles_on_client_swap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        relay, nc = self._relay_nc()
        sub = await self._bound_sub(relay)
        relay.connection_generation = 2  # a mid-session client replacement

        await self._drive_converse(relay, sub, monkeypatch)

        assert nc.subscribe.await_count == 2  # establish + reconcile re-bind
        assert sub._generation == 2  # bound to the new client's generation

    async def test_reconcile_noop_when_generation_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        relay, nc = self._relay_nc()
        sub = await self._bound_sub(relay)  # generation stays 1

        await self._drive_converse(relay, sub, monkeypatch)

        assert nc.subscribe.await_count == 1  # no re-subscribe — a no-op
        assert sub._generation == 1

    async def test_failed_resubscribe_does_not_crash_the_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        relay, _nc = self._relay_nc()
        sub = await self._bound_sub(relay)
        # A client swap forces the reconcile, but the redial is wedged.
        relay.connection_generation = 2
        relay.get_nc = AsyncMock(side_effect=TimeoutError("dial in progress"))

        await self._drive_converse(relay, sub, monkeypatch)  # must not raise

        assert sub._handle is None  # orphan dropped; nothing bound on a dead client
        assert sub._generation == 1  # generation unchanged — binds only on success

    async def test_transient_fetch_error_does_not_exit_the_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fetch raising mid-swap keeps the loop alive instead of crashing.

        The same client replacement the SUB reconcile survives can make the
        per-tick durable fetch (relay.fetch / get_nc) raise.  Unguarded that
        traceback propagates out of ``_talk_converse`` and exits ``biff talk``.
        The guard absorbs it; the loop paces on and re-fetches next tick.
        """
        relay, _nc = self._relay_nc()
        sub = await self._bound_sub(relay)

        fetch = AsyncMock(side_effect=[TimeoutError("dial in progress"), None])
        monkeypatch.setattr("biff.__main__._talk_fetch_and_print", fetch)
        monkeypatch.setattr(
            "biff.__main__._wait_for_input_or_notify",
            AsyncMock(side_effect=[_NO_INPUT, None]),
        )

        await asyncio.wait_for(
            _talk_converse(
                relay,
                sub,
                asyncio.Queue(),
                asyncio.Event(),
                "kai:kaihex01",
                "kai",
                "eric",
            ),
            timeout=5.0,
        )

        assert fetch.await_count == 2  # survived the error and retried next tick

    async def test_normal_fetch_renders_without_false_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A healthy fetch runs each tick and never trips the failure latch."""
        relay, _nc = self._relay_nc()
        sub = await self._bound_sub(relay)

        fetch = AsyncMock(side_effect=[None, None])
        monkeypatch.setattr("biff.__main__._talk_fetch_and_print", fetch)
        monkeypatch.setattr(
            "biff.__main__._wait_for_input_or_notify",
            AsyncMock(side_effect=[_NO_INPUT, None]),
        )

        with caplog.at_level(logging.DEBUG, logger="biff.__main__"):
            await asyncio.wait_for(
                _talk_converse(
                    relay,
                    sub,
                    asyncio.Queue(),
                    asyncio.Event(),
                    "kai:kaihex01",
                    "kai",
                    "eric",
                ),
                timeout=5.0,
            )

        assert fetch.await_count == 2  # fetched every tick, as before
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    async def test_loop_establishes_and_threads_sub_to_converse(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_talk_loop`` binds the SUB and hands it to the conversation loop.

        Guards the wiring hop: a future edit that drops the ``sub`` before
        ``_talk_converse`` would silently revert the standalone command to the
        orphaned-SUB bug while the leaf-level reconcile tests still pass.
        """
        relay, nc = self._relay_nc()
        captured: dict[str, object] = {}

        async def _fake_converse(
            _relay: object, sub: _TalkSubscription, *args: object, **kwargs: object
        ) -> None:
            captured["sub"] = sub

        def _eof_reader(
            input_queue: queue.Queue[str | None], _stop: threading.Event
        ) -> None:
            input_queue.put(None)  # signal EOF at once instead of reading stdin

        monkeypatch.setattr("biff.__main__._talk_converse", _fake_converse)
        monkeypatch.setattr("biff.__main__._stdin_reader", _eof_reader)

        await asyncio.wait_for(
            _talk_loop(relay, "kai:kaihex01", "kai", "eric"), timeout=5.0
        )

        sub = captured["sub"]
        assert isinstance(sub, _TalkSubscription)
        assert nc.subscribe.await_count == 1  # established before converse
        assert sub._generation == 1


class TestModalTalkReconcileWiring:
    """The production REPL path threads the talk SUB into the modal loops.

    ``TestModalTalkReconcile`` proves the leaf loops reconcile when handed a
    sub; these drive ``_handle_repl_talk`` — the ``_repl_loop`` entry point —
    end to end, so a dropped ``talk_sub`` kwarg at any hop (``_handle_repl_talk``
    → ``_initiate_talk`` → ``_wait_for_talk_accept``, and → ``_repl_talk``) fails
    a test instead of silently reverting the live REPL to the orphaned-SUB bug.
    """

    @staticmethod
    def _ctx() -> tuple[MagicMock, MagicMock, MagicMock]:
        nc = MagicMock()
        nc.subscribe = AsyncMock(side_effect=[AsyncMock(), AsyncMock()])
        nc.publish = AsyncMock()
        relay = MagicMock(spec=NatsRelay)
        relay.get_nc = AsyncMock(return_value=nc)
        relay.connection_generation = 1
        relay.talk_notify_subject = MagicMock(return_value="biff.talk.kai")
        relay.get_session = AsyncMock(return_value=None)
        relay.update_session = AsyncMock()
        relay.get_sessions_for_repos = AsyncMock(
            return_value=[
                UserSession(
                    user="jfreeman", tty="75abc665", tty_name="tty6", repo="myrepo"
                )
            ]
        )
        talk = TalkState(
            relay=cast("Relay", relay),
            user="kai",
            tty="kaihex01",
            session_key="kai:kaihex01",
            tty_name="tty1",
        )
        ctx = MagicMock()
        ctx.talk = talk
        ctx.user = "kai"
        ctx.tty_name = "tty1"
        ctx.session_key = "kai:kaihex01"
        ctx.relay = relay
        ctx.config.repo_name = "myrepo"
        ctx.visible_repos = frozenset({"myrepo"})
        return ctx, relay, nc

    @staticmethod
    async def _bound_sub(ctx: MagicMock) -> _ReplTalkSubscription:
        sub = _ReplTalkSubscription(ctx, asyncio.Event())
        await sub.establish()  # binds generation 1, first subscribe
        return sub

    async def _drive_handle(
        self,
        ctx: MagicMock,
        sub: _ReplTalkSubscription,
        args: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # One idle tick (where the reconcile fires), then ``end`` exits the loop.
        monkeypatch.setattr(
            "biff.__main__._wait_for_input_or_notify",
            AsyncMock(side_effect=[_NO_INPUT, "end"]),
        )
        await asyncio.wait_for(
            _handle_repl_talk(
                ctx,
                args,
                asyncio.Queue(),
                asyncio.Event(),
                threading.Event(),
                [""],
                "kai> ",
                ReplDisplay(),
                talk_sub=sub,
            ),
            timeout=5.0,
        )

    async def test_connected_loop_reconciles_on_client_swap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Responder path: ``_handle_repl_talk`` → ``_repl_talk`` re-binds."""
        ctx, relay, nc = self._ctx()
        # A pending invite makes this the responder → enters the connected loop.
        ctx.talk.receive(
            {
                "type": "invite",
                "from": "jfreeman",
                "from_tty": "tty6",
                "from_key": "jfreeman:75abc665",
                "body": "wants to talk",
                "to_key": "kai:kaihex01",
            }
        )
        ctx.talk.drain_idle()
        sub = await self._bound_sub(ctx)
        relay.connection_generation = 2  # a mid-talk client replacement

        await self._drive_handle(ctx, sub, ["@jfreeman"], monkeypatch)

        assert nc.subscribe.await_count == 2  # establish + reconcile re-bind
        assert sub._generation == 2

    async def test_accept_wait_reconciles_on_client_swap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Initiator path: ``_handle_repl_talk`` → accept-wait re-binds."""
        ctx, relay, nc = self._ctx()  # no pending invite → initiator
        sub = await self._bound_sub(ctx)
        relay.connection_generation = 2  # a client replacement while waiting

        await self._drive_handle(ctx, sub, ["@jfreeman:tty6"], monkeypatch)

        assert nc.subscribe.await_count == 2  # establish + reconcile re-bind
        assert sub._generation == 2


# ---------------------------------------------------------------------------
# _set_talk_plan / _clear_talk_plan — swallowed failures stay diagnosable
# ---------------------------------------------------------------------------


class TestTalkPlanBestEffortLogging:
    """Presence plan updates are best-effort but must not swallow the cause.

    A wedged relay must never crash the REPL over a cosmetic ``talking to …``
    update, so the get/update pair is guarded — but the DEBUG log carries
    ``exc_info`` so the underlying relay failure is recoverable from biff.log
    (it stays below the WARNING stderr floor, off the interactive terminal).
    """

    @staticmethod
    def _wedged_ctx() -> MagicMock:
        ctx = MagicMock()
        ctx.session_key = "kai:kaihex01"
        ctx.relay = MagicMock(spec=NatsRelay)
        ctx.relay.get_session = AsyncMock(side_effect=TimeoutError("relay wedged"))
        return ctx

    async def test_set_plan_failure_logs_with_exc_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        ctx = self._wedged_ctx()
        with caplog.at_level(logging.DEBUG, logger="biff.__main__"):
            await _set_talk_plan(ctx, "eric:tty2")  # must not raise
        records = [r for r in caplog.records if "talk plan" in r.getMessage()]
        assert records
        assert all(r.exc_info is not None for r in records)

    async def test_clear_plan_failure_logs_with_exc_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        ctx = self._wedged_ctx()
        with caplog.at_level(logging.DEBUG, logger="biff.__main__"):
            await _clear_talk_plan(ctx)  # must not raise
        records = [r for r in caplog.records if "talk plan" in r.getMessage()]
        assert records
        assert all(r.exc_info is not None for r in records)
