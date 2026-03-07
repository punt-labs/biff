"""Tests for REPL loop control flow (biff.__main__._repl_loop).

Coverage for the Z specification docs/repl.tex: prompt gate lifecycle,
mode transitions, dispatch outcomes, and notification sync.  Tests
feed lines into the asyncio queue and verify prompt gate state and
loop termination.  No NATS, no stdin thread.
"""

from __future__ import annotations

import asyncio
import threading as threading_mod
from unittest.mock import AsyncMock, patch

import pytest

from biff.__main__ import _repl_loop
from biff.cli_session import CliContext
from biff.commands import CommandResult
from biff.models import BiffConfig
from biff.relay import LocalRelay
from biff.repl_notify import NotifyState


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
        talk_q: asyncio.Queue[dict[str, str]] = asyncio.Queue()

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
                talk_q,
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
        talk_q: asyncio.Queue[dict[str, str]] = asyncio.Queue()

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
                talk_q,
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
        talk_q: asyncio.Queue[dict[str, str]] = asyncio.Queue()

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
                talk_q,
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
        talk_q: asyncio.Queue[dict[str, str]] = asyncio.Queue()

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
                talk_q,
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
        talk_q: asyncio.Queue[dict[str, str]] = asyncio.Queue()

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
                talk_q,
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
        talk_q: asyncio.Queue[dict[str, str]] = asyncio.Queue()

        with patch("biff.dispatch.dispatch", new_callable=AsyncMock) as mock_dispatch:
            await _repl_loop(
                ctx,
                notify,
                "prompt> ",
                q,
                asyncio.Event(),
                gate,
                ["prompt> "],
                talk_q,
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
        talk_q: asyncio.Queue[dict[str, str]] = asyncio.Queue()

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
                talk_q,
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
        talk_q: asyncio.Queue[dict[str, str]] = asyncio.Queue()

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
                talk_q,
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
        talk_q: asyncio.Queue[dict[str, str]] = asyncio.Queue()

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
                talk_q,
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
        talk_q: asyncio.Queue[dict[str, str]] = asyncio.Queue()

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
                talk_q,
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
        talk_q: asyncio.Queue[dict[str, str]] = asyncio.Queue()

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
                talk_q,
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
        talk_q: asyncio.Queue[dict[str, str]] = asyncio.Queue()

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
                talk_q,
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
        talk_q: asyncio.Queue[dict[str, str]] = asyncio.Queue()

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
                talk_q,
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
        talk_q: asyncio.Queue[dict[str, str]] = asyncio.Queue()

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
                talk_q,
            )

        mock_sync.assert_not_awaited()
