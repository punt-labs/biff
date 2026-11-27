"""``biff watch``: print one JSON line per arrival summary biffd pushes."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from typing import TYPE_CHECKING, Self, final

from biff.cli_daemon import DaemonCommand
from biff.client.connection import ConnectionLostError

if TYPE_CHECKING:
    from collections.abc import Callable

    from biff.client.connection import DaemonClient
    from biff.frames import Event

__all__ = ["WatchCommand"]

_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


@final
class WatchCommand:
    """Subscribes to biffd as this invocation's session and prints each event.

    A line is ``{"seq", "kind", "data"}``: counts and the active wall, never a
    message body.  The watch ends on SIGINT or SIGTERM, when stdout closes,
    and when the connection is lost.
    """

    __slots__ = ("_command", "_out")

    _command: DaemonCommand
    _out: Callable[[str], None]

    def __new__(cls, command: DaemonCommand, out: Callable[[str], None]) -> Self:
        """Watch as *command*'s session, handing each line to *out*.

        *out* raises ``BrokenPipeError`` when nothing is reading its output.
        """
        self = super().__new__(cls)
        self._command = command
        self._out = out
        return self

    @classmethod
    async def watch_this_process(
        cls, user_override: str | None, session_ref: str | None
    ) -> int:
        """Watch as this process's session, printing to stdout; return the status."""
        try:
            command = DaemonCommand.for_this_process(user_override, session_ref)
        except DaemonCommand.FAILURES as exc:
            return cls._failed(f"Error: {exc}")
        return await cls(command, cls._write_stdout).run()

    async def run(self) -> int:
        """Watch until the end and return the exit status."""
        try:
            client, _ = await self._command.open()
        except DaemonCommand.FAILURES as exc:
            return self._failed(f"Error: {exc}")
        try:
            return await self._watch(client)
        finally:
            await client.close()

    async def _watch(self, client: DaemonClient) -> int:
        try:
            await client.subscribe()
        except DaemonCommand.FAILURES as exc:
            return self._failed(f"Error: {exc}")
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in _STOP_SIGNALS:
            loop.add_signal_handler(sig, stop.set)
        stopped = asyncio.create_task(stop.wait())
        try:
            return await self._print_until(client, stopped)
        finally:
            for sig in _STOP_SIGNALS:
                loop.remove_signal_handler(sig)
            stopped.cancel()

    async def _print_until(
        self, client: DaemonClient, stopped: asyncio.Task[bool]
    ) -> int:
        while True:
            arrival = asyncio.create_task(client.next_event())
            await asyncio.wait({arrival, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if not arrival.done():
                arrival.cancel()
                return 0
            try:
                self._out(self._line(arrival.result()))
            except BrokenPipeError:
                return 0
            except ConnectionLostError as exc:
                return self._failed(f"biffd connection lost: {exc}")

    @staticmethod
    def _line(event: Event) -> str:
        return json.dumps({"seq": event.seq, "kind": event.kind, "data": event.data})

    @staticmethod
    def _failed(message: str) -> int:
        sys.stderr.write(message + "\n")
        sys.stderr.flush()
        return 1

    @staticmethod
    def _write_stdout(line: str) -> None:
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except BrokenPipeError:
            # Point stdout at nothing so the interpreter's exit flush is quiet.
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            raise
