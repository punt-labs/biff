"""The terminal a REPL or a talk reads from: lines on a thread, a prompt gate."""

from __future__ import annotations

import asyncio
import queue
import sys
import threading
from contextlib import suppress
from enum import Enum, auto
from typing import TYPE_CHECKING, Protocol, Self, final

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["Console", "PromptedConsole", "Tick"]

_WAKE_SECONDS = 2.0


class Tick(Enum):
    """Nothing was typed: the wait ended because something woke it, or timed out."""

    IDLE = auto()


type Reading = str | Tick | None
"""What the console gave: a line, a tick, or ``None`` at end of input."""


class Console(Protocol):
    """Where lines come from, and when the next prompt may appear."""

    @property
    def prompt(self) -> str:
        """The prompt the next read shows."""
        ...

    def set_prompt(self, prompt: str) -> None:
        """Show *prompt* from the next read on."""
        ...

    def release(self) -> None:
        """Let the next prompt appear, once output so far has reached the terminal."""
        ...

    def say(self, text: str = "", *, end: str = "\n") -> None:
        """Write *text* to the terminal's output."""
        ...

    def complain(self, text: str) -> None:
        """Write the line *text* to the terminal's error output."""
        ...

    async def next(self, wake: asyncio.Event) -> Reading:
        """Wait for a line, for *wake*, or for the idle timeout."""
        ...


@final
class PromptedConsole:
    """Reads lines with ``input(prompt)`` on a worker thread, so the loop stays free.

    The thread waits for the prompt gate before each prompt, so a prompt only
    appears after the loop has finished printing.  ``input`` flushes at once
    when the gate opens, so :meth:`release` flushes stdout first: otherwise the
    prompt overtakes buffered output and collides with the last line.
    """

    __slots__ = ("_gate", "_lines", "_prompt", "_pump", "_read", "_stop", "_typed")

    _prompt: str
    _read: Callable[[str], str]
    _gate: threading.Event
    _stop: threading.Event
    _typed: queue.Queue[str | None]
    _lines: asyncio.Queue[str | None]
    _pump: asyncio.Task[None] | None

    def __new__(cls, prompt: str, read: Callable[[str], str] = input) -> Self:
        """Create a console showing *prompt*; *read* reads one line (``input``)."""
        self = super().__new__(cls)
        self._prompt = prompt
        self._read = read
        self._gate = threading.Event()
        self._stop = threading.Event()
        self._typed = queue.Queue()
        self._lines = asyncio.Queue()
        self._pump = None
        return self

    @property
    def prompt(self) -> str:
        """The prompt the next read shows."""
        return self._prompt

    def set_prompt(self, prompt: str) -> None:
        """Show *prompt* from the next read on."""
        self._prompt = prompt

    def start(self) -> None:
        """Start reading; the first prompt may appear at once."""
        self._gate.set()
        threading.Thread(target=self._read_lines, daemon=True).start()
        self._pump = asyncio.create_task(self._bridge())

    async def stop(self) -> None:
        """Stop reading and unblock the thread and the bridge."""
        self._stop.set()
        self._gate.set()
        self._typed.put(None)
        if self._pump is not None:
            self._pump.cancel()
            with suppress(asyncio.CancelledError):
                await self._pump

    def release(self) -> None:
        """Flush stdout, then let the next prompt appear."""
        sys.stdout.flush()
        self._gate.set()

    def say(self, text: str = "", *, end: str = "\n") -> None:
        """Write *text* to stdout and flush it, so a prompt never overtakes it."""
        sys.stdout.write(text + end)
        sys.stdout.flush()

    def complain(self, text: str) -> None:
        """Write the line *text* to stderr."""
        sys.stderr.write(text + "\n")
        sys.stderr.flush()

    async def next(self, wake: asyncio.Event) -> Reading:
        """Wait for a line, for *wake*, or two seconds."""
        line = asyncio.create_task(self._lines.get())
        woken = asyncio.create_task(wake.wait())
        done, pending = await asyncio.wait(
            {line, woken}, return_when=asyncio.FIRST_COMPLETED, timeout=_WAKE_SECONDS
        )
        for task in pending:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if line in done:
            return line.result()
        return Tick.IDLE

    def _read_lines(self) -> None:
        """Read on the worker thread, one prompt per opening of the gate."""
        while not self._stop.is_set():
            self._gate.wait()
            if self._stop.is_set():
                return
            self._gate.clear()
            try:
                typed = self._read(self._prompt)
            except (EOFError, KeyboardInterrupt):
                self._typed.put(None)
                return
            self._typed.put(typed)

    async def _bridge(self) -> None:
        """Move each typed line from the thread's queue to the loop's."""
        loop = asyncio.get_running_loop()
        while True:
            typed = await loop.run_in_executor(None, self._typed.get)
            await self._lines.put(typed)
            if typed is None:
                break
