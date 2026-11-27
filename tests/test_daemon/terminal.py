"""A scripted terminal: a typist whose lines the console reads, and a screen."""

from __future__ import annotations

import asyncio
import queue
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

_WAIT = 5.0


@final
class Typist:
    """Stands in for ``input``: the console's reader thread blocks here for a line.

    Every prompt the console shows is recorded, so a test can tell that a prompt
    was held back until the output before it had been written.
    """

    def __init__(self) -> None:
        self._typed: queue.Queue[str | None] = queue.Queue()
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        try:
            line = self._typed.get(timeout=30)
        except queue.Empty:
            raise EOFError from None  # a test that left the reader waiting
        if line is None:
            raise EOFError
        return line

    def type(self, line: str) -> None:
        """Make *line* the next thing typed."""
        self._typed.put(line)

    def end_input(self) -> None:
        """Close the input, as Ctrl-D does."""
        self._typed.put(None)

    async def prompted(self, count: int) -> None:
        """Wait until the console has shown *count* prompts."""
        await eventually(lambda: len(self.prompts) >= count)


@final
class Screen:
    """Everything the console wrote to stdout and stderr so far."""

    def __init__(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._capsys = capsys
        self.out = ""
        self.err = ""

    def refresh(self) -> None:
        """Take in what was written since the last look."""
        seen = self._capsys.readouterr()
        self.out += seen.out
        self.err += seen.err

    async def shows(self, text: str) -> None:
        """Wait until *text* is on the screen."""

        def present() -> bool:
            self.refresh()
            return text in self.out

        try:
            await eventually(present)
        except TimeoutError:
            msg = f"{text!r} never appeared; the screen shows:\n{self.out}"
            raise AssertionError(msg) from None

    async def stays_without(self, text: str, *, for_seconds: float = 0.3) -> None:
        """Assert *text* does not appear within *for_seconds*."""
        await asyncio.sleep(for_seconds)
        self.refresh()
        assert text not in self.out, self.out


async def eventually(check: Callable[[], bool]) -> None:
    """Wait for *check* to hold, up to five seconds."""
    async with asyncio.timeout(_WAIT):
        while not check():
            await asyncio.sleep(0.01)
