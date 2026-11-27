"""The console: a prompt appears only after the output before it, EOF ends input."""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

import pytest

from biff.cli_console import PromptedConsole, Tick
from tests.test_daemon.terminal import Typist, eventually

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class _Stdout:
    """Records writes and flushes in order, with whatever else shares *events*."""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    def write(self, text: str) -> int:
        self._events.append(f"write {text!r}")
        return len(text)

    def flush(self) -> None:
        self._events.append("flush")


@pytest.fixture
async def console() -> AsyncIterator[tuple[PromptedConsole, Typist]]:
    typist = Typist()
    shown = PromptedConsole("biff> ", typist)
    shown.start()
    yield shown, typist
    typist.end_input()
    await shown.stop()


async def test_the_first_prompt_appears_at_once(
    console: tuple[PromptedConsole, Typist],
) -> None:
    _, typist = console
    await typist.prompted(1)
    assert typist.prompts == ["biff> "]


async def test_a_typed_line_is_returned(
    console: tuple[PromptedConsole, Typist],
) -> None:
    shown, typist = console
    typist.type("who")
    assert await shown.next(asyncio.Event()) == "who"


async def test_the_next_prompt_waits_for_release(
    console: tuple[PromptedConsole, Typist],
) -> None:
    shown, typist = console
    typist.type("who")
    await shown.next(asyncio.Event())
    await asyncio.sleep(0.1)
    assert len(typist.prompts) == 1  # no second prompt before release
    shown.release()
    await typist.prompted(2)


async def test_release_flushes_output_before_the_prompt_can_appear(
    console: tuple[PromptedConsole, Typist], monkeypatch: pytest.MonkeyPatch
) -> None:
    shown, typist = console
    events: list[str] = []
    monkeypatch.setattr(sys, "stdout", _Stdout(events))
    typist.type("who")
    await shown.next(asyncio.Event())
    # What the loop does after a command: write its output, then release.
    shown.say("table", end="")
    events.clear()
    shown.release()
    await eventually(lambda: len(typist.prompts) == 2)
    assert events[0] == "flush"


async def test_the_prompt_can_change_between_reads(
    console: tuple[PromptedConsole, Typist],
) -> None:
    shown, typist = console
    typist.type("talk")
    await shown.next(asyncio.Event())
    shown.set_prompt("you> ")
    shown.release()
    await typist.prompted(2)
    assert typist.prompts[1] == "you> "


async def test_end_of_input_is_none(console: tuple[PromptedConsole, Typist]) -> None:
    shown, typist = console
    typist.end_input()
    assert await shown.next(asyncio.Event()) is None


async def test_a_wake_ends_the_wait_with_a_tick(
    console: tuple[PromptedConsole, Typist],
) -> None:
    shown, _ = console
    wake = asyncio.Event()
    wake.set()
    assert await shown.next(wake) is Tick.IDLE


async def test_a_quiet_terminal_ticks_after_the_idle_timeout(
    console: tuple[PromptedConsole, Typist], monkeypatch: pytest.MonkeyPatch
) -> None:
    shown, _ = console
    monkeypatch.setattr("biff.cli_console._WAKE_SECONDS", 0.05)
    assert await shown.next(asyncio.Event()) is Tick.IDLE


async def test_a_line_typed_during_a_wait_is_not_lost_to_a_tick(
    console: tuple[PromptedConsole, Typist],
) -> None:
    shown, typist = console
    await typist.prompted(1)
    wake = asyncio.Event()
    waiting = asyncio.create_task(shown.next(wake))
    await asyncio.sleep(0.05)
    typist.type("kept")
    assert await waiting == "kept"


async def test_say_writes_to_stdout_and_complain_to_stderr(
    console: tuple[PromptedConsole, Typist], capsys: pytest.CaptureFixture[str]
) -> None:
    shown, _ = console
    shown.say("to stdout")
    shown.complain("to stderr")
    seen = capsys.readouterr()
    assert (seen.out, seen.err) == ("to stdout\n", "to stderr\n")


async def test_end_of_input_stays_end_of_input_for_every_later_read(
    console: tuple[PromptedConsole, Typist],
) -> None:
    """A talk loop that consumed the EOF must not leave the shell waiting."""
    shown, typist = console
    typist.end_input()
    assert await shown.next(asyncio.Event()) is None
    assert await asyncio.wait_for(shown.next(asyncio.Event()), timeout=1.0) is None
