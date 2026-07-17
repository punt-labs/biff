"""Tests for REPL loop control flow (biff.__main__._repl_loop).

Coverage for the Z specification docs/repl.tex: prompt gate lifecycle,
mode transitions, dispatch outcomes, and notification sync.  Tests
feed lines into the asyncio queue and verify prompt gate state and
loop termination.  No NATS, no stdin thread.
"""

from __future__ import annotations

import asyncio
import re
import threading as threading_mod
from unittest.mock import AsyncMock, patch

import pytest

from biff.__main__ import (
    _handle_timestamps,
    _poll_notify,
    _release_prompt,
    _repl_loop,
)
from biff.cli_session import CliContext
from biff.commands import CommandResult
from biff.models import BiffConfig
from biff.relay import LocalRelay
from biff.repl_display import ReplDisplay
from biff.repl_notify import NotifyState
from biff.talk_state import PENDING_INVITE_TTL


def _make_ctx(tmp_path: object) -> CliContext:
    """Build a CliContext with a LocalRelay for testing."""
    from pathlib import Path

    relay = LocalRelay(Path(str(tmp_path)))
    return CliContext(
        relay=relay,
        config=BiffConfig(user="kai", repo_name="test"),
        session_key="kai:abc12345",
        user="kai",
        tty="abc12345",
        tty_name="tty1",
    )


def _make_aqueue(
    lines: list[str | None],
) -> asyncio.Queue[str | None]:
    """Build an asyncio queue pre-loaded with lines.

    After the real lines, adds a None (EOF) sentinel so the loop
    terminates even if no explicit exit is provided.
    """
    q: asyncio.Queue[str | None] = asyncio.Queue()
    for line in lines:
        q.put_nowait(line)
    # Safety sentinel — ensures loop always terminates.
    q.put_nowait(None)
    return q


class _RecordingStdout:
    """stdout stand-in that appends "flush" to a shared event log.

    Non-empty writes append "write"; ``flush()`` appends "flush".  Used to
    prove command output is flushed before the prompt gate is released.
    """

    def __init__(self, log: list[str]) -> None:
        self._log = log

    def write(self, s: str) -> int:
        if s.strip():
            self._log.append("write")
        return len(s)

    def flush(self) -> None:
        self._log.append("flush")


class _RecordingGate:
    """threading.Event wrapper that appends "gate_set" to a shared log.

    Implements only the surface ``_repl_loop`` touches on the prompt gate
    (``set``/``is_set``) plus the ``clear``/``wait`` an Event would expose.
    """

    def __init__(self, log: list[str]) -> None:
        self._event = threading_mod.Event()
        self._log = log

    def set(self) -> None:
        self._log.append("gate_set")
        self._event.set()

    def clear(self) -> None:
        self._event.clear()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def is_set(self) -> bool:
        return self._event.is_set()


# -----------------------------------------------------------------------
# Prompt gate lifecycle
# -----------------------------------------------------------------------


class TestPromptGateLifecycle:
    """Z spec: promptGateOpen transitions."""

    @pytest.mark.anyio()
    async def test_gate_set_after_command_output(self, tmp_path: object) -> None:
        """OutputComplete: promptGateOpen' = ztrue after command."""
        ctx = _make_ctx(tmp_path)
        gate = threading_mod.Event()
        gate.clear()  # Start closed
        notify = NotifyState()
        aqueue = _make_aqueue(["who"])

        with patch("biff.dispatch.dispatch", new_callable=AsyncMock) as mock_dispatch:
            mock_dispatch.return_value = CommandResult(text="output")
            await _repl_loop(
                ctx,
                notify,
                "prompt> ",
                aqueue,
                asyncio.Event(),
                gate,
                ["prompt> "],
            )

        # After the command completes, the gate should be set
        # (allowing the stdin thread to print the next prompt).
        assert gate.is_set()

    @pytest.mark.anyio()
    async def test_gate_set_after_empty_line(self, tmp_path: object) -> None:
        """DispatchEmpty: promptGateOpen' = ztrue for blank input."""
        ctx = _make_ctx(tmp_path)
        gate = threading_mod.Event()
        gate.clear()
        notify = NotifyState()
        aqueue = _make_aqueue([""])

        with patch("biff.dispatch.dispatch", new_callable=AsyncMock) as mock_dispatch:
            mock_dispatch.return_value = CommandResult(text="")
            await _repl_loop(
                ctx,
                notify,
                "prompt> ",
                aqueue,
                asyncio.Event(),
                gate,
                ["prompt> "],
            )

        assert gate.is_set()

    @pytest.mark.anyio()
    async def test_gate_set_after_error(self, tmp_path: object) -> None:
        """DispatchError: promptGateOpen' = ztrue after ValueError."""
        ctx = _make_ctx(tmp_path)
        gate = threading_mod.Event()
        gate.clear()
        notify = NotifyState()
        aqueue = _make_aqueue(["badcmd"])

        with patch("biff.dispatch.dispatch", new_callable=AsyncMock) as mock_dispatch:
            mock_dispatch.side_effect = ValueError("boom")
            await _repl_loop(
                ctx,
                notify,
                "prompt> ",
                aqueue,
                asyncio.Event(),
                gate,
                ["prompt> "],
            )

        assert gate.is_set()

    @pytest.mark.anyio()
    async def test_gate_set_after_talk(self, tmp_path: object) -> None:
        """ExitTalkMode: promptGateOpen' = ztrue after talk returns."""
        ctx = _make_ctx(tmp_path)
        gate = threading_mod.Event()
        gate.clear()
        notify = NotifyState()
        aqueue = _make_aqueue(["talk @eric"])

        with (
            patch("biff.__main__._handle_repl_talk", new_callable=AsyncMock),
            patch("biff.dispatch.dispatch", new_callable=AsyncMock),
        ):
            await _repl_loop(
                ctx,
                notify,
                "prompt> ",
                aqueue,
                asyncio.Event(),
                gate,
                ["prompt> "],
            )

        assert gate.is_set()


# -----------------------------------------------------------------------
# Dispatch outcomes
# -----------------------------------------------------------------------


class TestDispatchOutcomes:
    """Z spec: DispatchCommand, DispatchExit, DispatchEmpty, DispatchError."""

    @pytest.mark.anyio()
    async def test_exit_terminates_loop(self, tmp_path: object) -> None:
        """DispatchExit: dispatch returns None for exit → loop breaks."""
        ctx = _make_ctx(tmp_path)
        gate = threading_mod.Event()
        gate.set()
        notify = NotifyState()
        # Put "exit" — dispatch returns None.
        aqueue = _make_aqueue(["exit"])

        with patch("biff.dispatch.dispatch", new_callable=AsyncMock) as mock_dispatch:
            mock_dispatch.return_value = None  # exit signal
            await _repl_loop(
                ctx,
                notify,
                "prompt> ",
                aqueue,
                asyncio.Event(),
                gate,
                ["prompt> "],
            )

        mock_dispatch.assert_awaited_once()

    @pytest.mark.anyio()
    async def test_eof_terminates_loop(self, tmp_path: object) -> None:
        """InputReady with None (EOF) → loop breaks."""
        ctx = _make_ctx(tmp_path)
        gate = threading_mod.Event()
        gate.set()
        notify = NotifyState()
        # Put None directly — simulates EOF.
        q: asyncio.Queue[str | None] = asyncio.Queue()
        q.put_nowait(None)

        with patch("biff.dispatch.dispatch", new_callable=AsyncMock) as mock_dispatch:
            await _repl_loop(
                ctx,
                notify,
                "prompt> ",
                q,
                asyncio.Event(),
                gate,
                ["prompt> "],
            )

        # dispatch should NOT have been called (EOF before any input).
        mock_dispatch.assert_not_awaited()

    @pytest.mark.anyio()
    async def test_command_output_printed(
        self, tmp_path: object, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """DispatchCommand: command output is printed."""
        ctx = _make_ctx(tmp_path)
        gate = threading_mod.Event()
        gate.set()
        notify = NotifyState()
        aqueue = _make_aqueue(["who"])

        with patch("biff.dispatch.dispatch", new_callable=AsyncMock) as mock_dispatch:
            mock_dispatch.return_value = CommandResult(text="3 online")
            await _repl_loop(
                ctx,
                notify,
                "prompt> ",
                aqueue,
                asyncio.Event(),
                gate,
                ["prompt> "],
            )

        captured = capsys.readouterr()
        assert "3 online" in captured.out

    @pytest.mark.anyio()
    async def test_error_printed_to_stderr(
        self, tmp_path: object, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """DispatchError: ValueError printed to stderr."""
        ctx = _make_ctx(tmp_path)
        gate = threading_mod.Event()
        gate.set()
        notify = NotifyState()
        aqueue = _make_aqueue(["badcmd"])

        with patch("biff.dispatch.dispatch", new_callable=AsyncMock) as mock_dispatch:
            mock_dispatch.side_effect = ValueError("no relay")
            await _repl_loop(
                ctx,
                notify,
                "prompt> ",
                aqueue,
                asyncio.Event(),
                gate,
                ["prompt> "],
            )

        captured = capsys.readouterr()
        assert "no relay" in captured.err


# -----------------------------------------------------------------------
# Mode transitions
# -----------------------------------------------------------------------


class TestModeTransitions:
    """Z spec: EnterTalkMode, ExitTalkMode."""

    @pytest.mark.anyio()
    async def test_talk_command_calls_handle(self, tmp_path: object) -> None:
        """EnterTalkMode: 'talk @user' delegates to _handle_repl_talk."""
        ctx = _make_ctx(tmp_path)
        gate = threading_mod.Event()
        gate.set()
        notify = NotifyState()
        aqueue = _make_aqueue(["talk @eric hello"])

        with (
            patch(
                "biff.__main__._handle_repl_talk", new_callable=AsyncMock
            ) as mock_talk,
            patch("biff.dispatch.dispatch", new_callable=AsyncMock),
        ):
            await _repl_loop(
                ctx,
                notify,
                "prompt> ",
                aqueue,
                asyncio.Event(),
                gate,
                ["prompt> "],
            )

        mock_talk.assert_awaited_once()
        # Verify args[0] is the tokens after "talk"
        call_args = mock_talk.call_args[0]
        assert call_args[0] is ctx
        assert "@eric" in call_args[1] or "eric" in str(call_args[1])

    @pytest.mark.anyio()
    async def test_talk_command_case_insensitive(self, tmp_path: object) -> None:
        """EnterTalkMode: 'TALK @user' also enters talk mode."""
        ctx = _make_ctx(tmp_path)
        gate = threading_mod.Event()
        gate.set()
        notify = NotifyState()
        aqueue = _make_aqueue(["TALK @eric"])

        with (
            patch(
                "biff.__main__._handle_repl_talk", new_callable=AsyncMock
            ) as mock_talk,
            patch("biff.dispatch.dispatch", new_callable=AsyncMock),
        ):
            await _repl_loop(
                ctx,
                notify,
                "prompt> ",
                aqueue,
                asyncio.Event(),
                gate,
                ["prompt> "],
            )

        mock_talk.assert_awaited_once()

    @pytest.mark.anyio()
    async def test_non_talk_command_dispatched(self, tmp_path: object) -> None:
        """'who' goes to dispatch, not _handle_repl_talk."""
        ctx = _make_ctx(tmp_path)
        gate = threading_mod.Event()
        gate.set()
        notify = NotifyState()
        aqueue = _make_aqueue(["who"])

        with (
            patch(
                "biff.__main__._handle_repl_talk", new_callable=AsyncMock
            ) as mock_talk,
            patch("biff.dispatch.dispatch", new_callable=AsyncMock) as mock_dispatch,
        ):
            mock_dispatch.return_value = CommandResult(text="output")
            await _repl_loop(
                ctx,
                notify,
                "prompt> ",
                aqueue,
                asyncio.Event(),
                gate,
                ["prompt> "],
            )

        mock_dispatch.assert_awaited_once()
        mock_talk.assert_not_awaited()


# -----------------------------------------------------------------------
# Notification sync (self-notification prevention)
# -----------------------------------------------------------------------


class TestNotificationSync:
    """Z spec: OutputComplete syncs lastUnread = currentUnread."""

    @pytest.mark.anyio()
    async def test_sync_after_command(self, tmp_path: object) -> None:
        """After command, notify state is synced (self-notification prevention)."""
        ctx = _make_ctx(tmp_path)
        gate = threading_mod.Event()
        gate.set()
        notify = NotifyState()
        aqueue = _make_aqueue(["who"])

        with (
            patch("biff.dispatch.dispatch", new_callable=AsyncMock) as mock_dispatch,
            patch("biff.__main__._sync_notify", new_callable=AsyncMock) as mock_sync,
        ):
            mock_dispatch.return_value = CommandResult(text="output")
            await _repl_loop(
                ctx,
                notify,
                "prompt> ",
                aqueue,
                asyncio.Event(),
                gate,
                ["prompt> "],
            )

        mock_sync.assert_awaited_once()

    @pytest.mark.anyio()
    async def test_no_sync_after_error(self, tmp_path: object) -> None:
        """After ValueError, sync is NOT called (error, not success)."""
        ctx = _make_ctx(tmp_path)
        gate = threading_mod.Event()
        gate.set()
        notify = NotifyState()
        aqueue = _make_aqueue(["badcmd"])

        with (
            patch("biff.dispatch.dispatch", new_callable=AsyncMock) as mock_dispatch,
            patch("biff.__main__._sync_notify", new_callable=AsyncMock) as mock_sync,
        ):
            mock_dispatch.side_effect = ValueError("boom")
            await _repl_loop(
                ctx,
                notify,
                "prompt> ",
                aqueue,
                asyncio.Event(),
                gate,
                ["prompt> "],
            )

        mock_sync.assert_not_awaited()

    @pytest.mark.anyio()
    async def test_no_sync_after_exit(self, tmp_path: object) -> None:
        """After exit, sync is NOT called (loop terminates)."""
        ctx = _make_ctx(tmp_path)
        gate = threading_mod.Event()
        gate.set()
        notify = NotifyState()
        aqueue = _make_aqueue(["exit"])

        with (
            patch("biff.dispatch.dispatch", new_callable=AsyncMock) as mock_dispatch,
            patch("biff.__main__._sync_notify", new_callable=AsyncMock) as mock_sync,
        ):
            mock_dispatch.return_value = None
            await _repl_loop(
                ctx,
                notify,
                "prompt> ",
                aqueue,
                asyncio.Event(),
                gate,
                ["prompt> "],
            )

        mock_sync.assert_not_awaited()


# -----------------------------------------------------------------------
# Multi-command sequences
# -----------------------------------------------------------------------


class TestMultiCommandSequence:
    """Verify prompt gate opens/closes across multiple commands."""

    @pytest.mark.anyio()
    async def test_two_commands_gate_set_after_each(self, tmp_path: object) -> None:
        """Gate reopens after each command in a sequence."""
        ctx = _make_ctx(tmp_path)
        gate = threading_mod.Event()
        gate.set()
        notify = NotifyState()
        aqueue = _make_aqueue(["who", "status"])

        call_count = 0

        async def _mock_dispatch(line: str, ctx: CliContext) -> CommandResult:
            nonlocal call_count
            call_count += 1
            return CommandResult(text=f"output-{call_count}")

        with patch("biff.dispatch.dispatch", side_effect=_mock_dispatch):
            await _repl_loop(
                ctx,
                notify,
                "prompt> ",
                aqueue,
                asyncio.Event(),
                gate,
                ["prompt> "],
            )

        assert call_count == 2
        assert gate.is_set()

    @pytest.mark.anyio()
    async def test_command_then_exit(self, tmp_path: object) -> None:
        """Command followed by exit: dispatch called twice."""
        ctx = _make_ctx(tmp_path)
        gate = threading_mod.Event()
        gate.set()
        notify = NotifyState()
        q: asyncio.Queue[str | None] = asyncio.Queue()
        q.put_nowait("who")
        q.put_nowait("exit")

        results: list[CommandResult | None] = [
            CommandResult(text="3 online"),
            None,
        ]
        call_idx = 0

        async def _mock_dispatch(line: str, ctx: CliContext) -> CommandResult | None:
            nonlocal call_idx
            r = results[call_idx]
            call_idx += 1
            return r

        with patch("biff.dispatch.dispatch", side_effect=_mock_dispatch):
            await _repl_loop(
                ctx,
                notify,
                "prompt> ",
                q,
                asyncio.Event(),
                gate,
                ["prompt> "],
            )

        assert call_idx == 2

    @pytest.mark.anyio()
    async def test_error_then_command(
        self, tmp_path: object, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Error followed by successful command: both run, gate set."""
        ctx = _make_ctx(tmp_path)
        gate = threading_mod.Event()
        gate.set()
        notify = NotifyState()
        aqueue = _make_aqueue(["bad", "who"])

        call_idx = 0

        async def _mock_dispatch(line: str, ctx: CliContext) -> CommandResult:
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                raise ValueError("unknown")
            return CommandResult(text="ok")

        with patch("biff.dispatch.dispatch", side_effect=_mock_dispatch):
            await _repl_loop(
                ctx,
                notify,
                "prompt> ",
                aqueue,
                asyncio.Event(),
                gate,
                ["prompt> "],
            )

        assert call_idx == 2
        captured = capsys.readouterr()
        assert "unknown" in captured.err
        assert "ok" in captured.out
        assert gate.is_set()

    @pytest.mark.anyio()
    async def test_talk_then_command(self, tmp_path: object) -> None:
        """Talk mode then normal command: both handled correctly."""
        ctx = _make_ctx(tmp_path)
        gate = threading_mod.Event()
        gate.set()
        notify = NotifyState()
        aqueue = _make_aqueue(["talk @eric", "who"])

        with (
            patch(
                "biff.__main__._handle_repl_talk",
                new_callable=AsyncMock,
            ) as mock_talk,
            patch("biff.dispatch.dispatch", new_callable=AsyncMock) as mock_dispatch,
        ):
            mock_dispatch.return_value = CommandResult(text="output")
            await _repl_loop(
                ctx,
                notify,
                "prompt> ",
                aqueue,
                asyncio.Event(),
                gate,
                ["prompt> "],
            )

        mock_talk.assert_awaited_once()
        mock_dispatch.assert_awaited_once()
        assert gate.is_set()


# -----------------------------------------------------------------------
# Timestamps toggle (biff-4uq)
# -----------------------------------------------------------------------


class TestTimestampsCommand:
    """REPL-only ``timestamps on|off`` toggle."""

    def test_handle_timestamps_on(self, capsys: pytest.CaptureFixture[str]) -> None:
        display = ReplDisplay()
        _handle_timestamps(["on"], display)
        assert display.show_timestamps is True
        assert "Timestamps on." in capsys.readouterr().out

    def test_handle_timestamps_off(self, capsys: pytest.CaptureFixture[str]) -> None:
        display = ReplDisplay()
        display.set_timestamps(on=True)
        _handle_timestamps(["off"], display)
        assert display.show_timestamps is False
        assert "Timestamps off." in capsys.readouterr().out

    def test_handle_timestamps_usage_on_bad_arg(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        display = ReplDisplay()
        _handle_timestamps(["maybe"], display)
        assert display.show_timestamps is False  # unchanged
        assert "Usage: timestamps on|off" in capsys.readouterr().out

    def test_handle_timestamps_usage_on_missing_arg(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        display = ReplDisplay()
        _handle_timestamps([], display)
        assert "Usage: timestamps on|off" in capsys.readouterr().out

    @pytest.mark.anyio()
    async def test_loop_routes_timestamps_command(self, tmp_path: object) -> None:
        """`timestamps on` in the loop toggles the session display."""
        ctx = _make_ctx(tmp_path)
        gate = threading_mod.Event()
        gate.set()
        notify = NotifyState()
        display = ReplDisplay()
        aqueue = _make_aqueue(["timestamps on"])

        with patch("biff.dispatch.dispatch", new_callable=AsyncMock) as mock_dispatch:
            await _repl_loop(
                ctx,
                notify,
                "prompt> ",
                aqueue,
                asyncio.Event(),
                gate,
                ["prompt> "],
                display=display,
            )

        assert display.show_timestamps is True
        # `timestamps` is a REPL built-in — never routed through dispatch.
        mock_dispatch.assert_not_awaited()

    @pytest.mark.anyio()
    async def test_toggle_then_talk_message_is_stamped(self, tmp_path: object) -> None:
        """End-to-end: typing `timestamps on` stamps subsequent talk output.

        Off → the talk renderer emits no stamp; after the loop processes
        `timestamps on`, the SAME display object makes the renderer prefix
        incoming messages with ``[HH:MM]``.  This connects the command path
        (`_repl_loop`) to the render path (`_format_talk_lines`) on one
        display, showing the user-observable behavior change (biff-4uq).
        """
        from biff.__main__ import _format_talk_lines
        from biff.talk_types import TalkNotification

        display = ReplDisplay()
        talk_msg = TalkNotification(
            ntype="message",
            nfrom="eric",
            nfrom_tty="tty2",
            nfrom_key="eric:def67890",
            nto="",
            nbody="on it now",
        )

        # Before: a received talk message renders without a stamp.
        lines_before = _format_talk_lines([talk_msg], display)
        assert re.search(r"\[\d{2}:\d{2}\]", lines_before[0]) is None

        # User types `timestamps on` at the REPL prompt.
        ctx = _make_ctx(tmp_path)
        gate = threading_mod.Event()
        gate.set()
        aqueue = _make_aqueue(["timestamps on"])
        with patch("biff.dispatch.dispatch", new_callable=AsyncMock):
            await _repl_loop(
                ctx,
                NotifyState(),
                "prompt> ",
                aqueue,
                asyncio.Event(),
                gate,
                ["prompt> "],
                display=display,
            )

        # After: the same display makes the renderer stamp the message.
        lines_after = _format_talk_lines([talk_msg], display)
        assert (
            re.search(r"▶  \[\d{2}:\d{2}\] eric:tty2  on it now", lines_after[0])
            is not None
        )


# -----------------------------------------------------------------------
# Prompt/output ordering (biff-1xt5)
# -----------------------------------------------------------------------


class TestPromptOutputOrdering:
    """Regression: command output must be flushed before the prompt gate opens.

    The stdin thread prints the next prompt via ``input()`` (which flushes
    immediately) as soon as ``prompt_gate.set()`` releases it.  If the
    command output printed by ``_repl_loop`` is not flushed first, the
    prompt overtakes the still-buffered output and collides with its first
    line.  Every gate release routes through ``_release_prompt`` which flushes
    first; this guards the whole bug class.  See biff-1xt5 and docs/repl.tex
    prompt-gate synchronization.
    """

    def test_release_prompt_flushes_before_set(self) -> None:
        """_release_prompt flushes stdout before opening the gate."""
        log: list[str] = []
        gate = _RecordingGate(log)
        with patch("sys.stdout", _RecordingStdout(log)):
            _release_prompt(gate)  # type: ignore[arg-type]
        assert log == ["flush", "gate_set"], f"flush must precede gate release: {log}"

    @pytest.mark.anyio()
    async def test_output_flushed_before_gate_released(self, tmp_path: object) -> None:
        """The command output flush must precede prompt_gate.set()."""
        ctx = _make_ctx(tmp_path)
        log: list[str] = []
        gate = _RecordingGate(log)
        notify = NotifyState()
        aqueue = _make_aqueue(["who"])

        with (
            patch("biff.dispatch.dispatch", new_callable=AsyncMock) as mock_dispatch,
            patch("biff.__main__._sync_notify", new_callable=AsyncMock),
            patch("sys.stdout", _RecordingStdout(log)),
        ):
            mock_dispatch.return_value = CommandResult(text="kai:tty1 online")
            await _repl_loop(
                ctx,
                notify,
                "prompt> ",
                aqueue,
                asyncio.Event(),
                gate,  # type: ignore[arg-type]
                ["prompt> "],
            )

        # A flush must have happened...
        assert "flush" in log, f"command output was never flushed: {log}"
        # ...and it must precede the prompt-gate release for this command.
        assert log.index("flush") < log.index("gate_set"), (
            f"prompt gate released before output flush: {log}"
        )
        # ...and the flush must follow the output write (order sanity).
        assert log.index("write") < log.index("flush"), (
            f"flush recorded before output write: {log}"
        )

    @pytest.mark.anyio()
    async def test_no_flush_gap_when_no_output(self, tmp_path: object) -> None:
        """Empty command output still releases the gate (no spurious flush req)."""
        ctx = _make_ctx(tmp_path)
        log: list[str] = []
        gate = _RecordingGate(log)
        notify = NotifyState()
        aqueue = _make_aqueue([""])

        with (
            patch("biff.dispatch.dispatch", new_callable=AsyncMock) as mock_dispatch,
            patch("biff.__main__._sync_notify", new_callable=AsyncMock),
            patch("sys.stdout", _RecordingStdout(log)),
        ):
            mock_dispatch.return_value = CommandResult(text="")
            await _repl_loop(
                ctx,
                notify,
                "prompt> ",
                aqueue,
                asyncio.Event(),
                gate,  # type: ignore[arg-type]
                ["prompt> "],
            )

        # No output → nothing to flush, but the gate must still open.
        assert "gate_set" in log
        assert "write" not in log


# -----------------------------------------------------------------------
# Invite-cancel withdraw parity (notification.tex WithdrawArrive)
# -----------------------------------------------------------------------


class TestInviteCancelWithdraw:
    """Cancelling an outgoing REPL invite withdraws it, not ends it.

    Abandoning an invite while still INVITING publishes ``ntWithdraw`` so
    the invitee's ``[TALK]`` marker clears at once (notification.tex
    WithdrawArrive), matching the MCP ``talk_end`` path.  A connected hangup
    stays an ``end`` frame (DrainEnd) — that path is unchanged.
    """

    @pytest.mark.anyio()
    async def test_cancel_while_inviting_withdraws(self, tmp_path: object) -> None:
        from biff.__main__ import _talk_handshake
        from biff.talk_state import TalkState
        from biff.talk_types import AcceptOutcome, TalkPhase

        ctx = _make_ctx(tmp_path)
        ctx.talk.begin_invite(
            partner="eric", partner_tty="tty2", partner_key="eric:def67890"
        )
        gate = threading_mod.Event()
        with (
            patch(
                "biff.__main__._wait_for_talk_accept",
                new_callable=AsyncMock,
                return_value=AcceptOutcome.NONE,
            ),
            patch.object(
                TalkState, "send_withdraw", new_callable=AsyncMock
            ) as spy_withdraw,
            patch.object(TalkState, "send_end", new_callable=AsyncMock) as spy_end,
        ):
            proceed = await _talk_handshake(
                ctx,
                "eric",
                "eric:def67890",
                "eric:tty2",
                ["talk", "@eric"],
                responding=False,
                aqueue=_make_aqueue([]),
                notify_event=asyncio.Event(),
                prompt_gate=gate,
            )

        assert proceed is False
        spy_withdraw.assert_awaited_once_with(to_key="eric:def67890")
        spy_end.assert_not_awaited()
        # The cancel returns us to idle (talk.tex LocalEnd).
        assert ctx.talk.phase is TalkPhase.IDLE

    @pytest.mark.anyio()
    async def test_ctrl_c_while_inviting_withdraws(self, tmp_path: object) -> None:
        """A Ctrl-C during the inviting-wait fires the withdraw, then exits.

        ``asyncio.run`` cancels the main task on SIGINT, so the accept-wait
        raises ``CancelledError`` (not ``KeyboardInterrupt``). The handshake
        clears the invitee's marker on the fast path (ntWithdraw) and re-raises
        so the cancellation propagates and the process exits to the shell.
        """
        from biff.__main__ import _talk_handshake
        from biff.talk_state import TalkState
        from biff.talk_types import TalkPhase

        ctx = _make_ctx(tmp_path)
        ctx.talk.begin_invite(
            partner="eric", partner_tty="tty2", partner_key="eric:def67890"
        )
        gate = threading_mod.Event()
        with (
            patch(
                "biff.__main__._wait_for_talk_accept",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError,
            ),
            patch.object(
                TalkState, "send_withdraw", new_callable=AsyncMock
            ) as spy_withdraw,
            patch.object(TalkState, "send_end", new_callable=AsyncMock) as spy_end,
            pytest.raises(asyncio.CancelledError),
        ):
            await _talk_handshake(
                ctx,
                "eric",
                "eric:def67890",
                "eric:tty2",
                ["talk", "@eric"],
                responding=False,
                aqueue=_make_aqueue([]),
                notify_event=asyncio.Event(),
                prompt_gate=gate,
            )

        spy_withdraw.assert_awaited_once_with(to_key="eric:def67890")
        spy_end.assert_not_awaited()
        # The withdraw completes before the re-raise, returning us to idle.
        assert ctx.talk.phase is TalkPhase.IDLE

    @pytest.mark.anyio()
    async def test_withdraw_publish_failure_still_resets(
        self, tmp_path: object
    ) -> None:
        """A wedged relay on withdraw must not strand the invite or leak a trace.

        When ``send_withdraw`` raises (relay disconnected, including the Ctrl-C
        cancel path), the local state still resets — no exception escapes and the
        invitee falls back to the pending-invite TTL sweep.
        """
        from biff.__main__ import _withdraw_talk_invite
        from biff.talk_state import TalkState
        from biff.talk_types import TalkPhase

        ctx = _make_ctx(tmp_path)
        ctx.talk.begin_invite(
            partner="eric", partner_tty="tty2", partner_key="eric:def67890"
        )
        with patch.object(
            TalkState,
            "send_withdraw",
            new_callable=AsyncMock,
            side_effect=TimeoutError("relay wedged"),
        ):
            await _withdraw_talk_invite(ctx, "eric", "eric:def67890")

        # No exception escaped; local state reset despite the failed publish.
        assert ctx.talk.phase is TalkPhase.IDLE


class TestInvitingWaitInputGate:
    """The inviting-wait must open the prompt gate so a typed line is read.

    Without releasing the gate the stdin thread stays parked at
    ``prompt_gate.wait()`` and never calls ``input()``, so a typed ``end``
    never reaches the cancel check — the REPL-cancel bug this guards.
    """

    @pytest.mark.anyio()
    async def test_wait_for_accept_opens_gate_before_reading(
        self, tmp_path: object
    ) -> None:
        from biff.__main__ import _wait_for_talk_accept
        from biff.talk_types import AcceptOutcome

        ctx = _make_ctx(tmp_path)
        gate = threading_mod.Event()  # starts closed, as after the stdin read
        aqueue = _make_aqueue(["end"])

        outcome = await _wait_for_talk_accept(ctx, aqueue, asyncio.Event(), gate)

        assert outcome is AcceptOutcome.NONE
        assert gate.is_set(), "gate must open so the stdin thread reads the line"


class TestPollNotifyExpiresInvites:
    """The REPL idle tick must age out stranded pending invites (CR-4).

    Only the MCP poller called ``expire_stale_invites``; the REPL tick drained
    banners but never reaped, so a crashed inviter's ``[TALK]`` marker never
    cleared in the REPL.  The tick now mirrors the server's ``_active_tick``.
    """

    @pytest.mark.anyio()
    async def test_repl_tick_ages_out_stale_invite(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _make_ctx(tmp_path)
        monkeypatch.setattr("biff.talk_state.time.monotonic", lambda: 0.0)
        ctx.talk.receive(
            {
                "type": "invite",
                "from": "eric",
                "from_tty": "tty2",
                "from_key": "eric:def67890",
                "body": "wants to talk",
                "to_key": "kai:abc12345",
            }
        )
        ctx.talk.drain_idle()  # record the pending invite at t=0
        assert "eric" in ctx.talk.pending_invites

        # The inviter never returns; the tick fires well past the TTL.
        monkeypatch.setattr(
            "biff.talk_state.time.monotonic", lambda: PENDING_INVITE_TTL + 1.0
        )
        await _poll_notify(ctx, NotifyState(), "prompt> ")

        assert "eric" not in ctx.talk.pending_invites  # reaped by the tick
