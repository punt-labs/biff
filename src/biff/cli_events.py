"""The events biffd pushes to an interactive command, collected for its loop."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, Self, final

from biff.client.connection import ConnectionLostError

if TYPE_CHECKING:
    from biff.client.connection import DaemonClient
    from biff.frames import Event

__all__ = ["EventPump"]

logger = logging.getLogger(__name__)


@final
class EventPump:
    """Reads a subscribed connection's events in the background.

    The loop that owns the terminal waits on :attr:`wake`, then takes what
    arrived: it never blocks on the connection, and a lost connection is a
    state it can read, not an exception raised in the middle of a prompt.
    """

    __slots__ = ("_client", "_lost", "_pending", "_task", "_wake")

    _client: DaemonClient
    _pending: list[Event]
    _lost: str
    _wake: asyncio.Event
    _task: asyncio.Task[None] | None

    def __new__(cls, client: DaemonClient) -> Self:
        """Create a pump over *client*, which has already subscribed."""
        self = super().__new__(cls)
        self._client = client
        self._pending = []
        self._lost = ""
        self._wake = asyncio.Event()
        self._task = None
        return self

    @property
    def wake(self) -> asyncio.Event:
        """Set whenever an event arrives or the connection is lost."""
        return self._wake

    @property
    def lost(self) -> str:
        """Why the connection ended, or the empty string while it lives."""
        return self._lost

    def start(self) -> None:
        """Begin reading events."""
        self._task = asyncio.create_task(self._read())

    async def stop(self) -> None:
        """Stop reading."""
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    def take(self) -> list[Event]:
        """Return the events that arrived since the last take, oldest first."""
        arrived, self._pending = self._pending, []
        return arrived

    def discard_talk(self) -> None:
        """Forget the talk notices that have arrived; keep mail and wall events.

        A conversation reads what its notices announce with ``talk_read``, so the
        notices themselves are spent; left alone they would pile up for as long as
        the talk lasts.
        """
        self._pending = [e for e in self._pending if not e.kind.startswith("talk_")]

    async def _read(self) -> None:
        try:
            while True:
                event = await self._client.next_event()
                # Looked up after the wait: take() replaces the list meanwhile.
                self._pending.append(event)
                self._wake.set()
        except ConnectionLostError as exc:
            self._lost = str(exc)
            self._wake.set()
        except Exception as exc:
            # The loop that owns the terminal waits on the wake, so a reader that
            # dies for any reason must wake it and say so; otherwise the shell
            # would wait for ever on a connection nobody reads.
            logger.exception("the event pump failed")
            self._lost = f"{type(exc).__name__}: {exc}"
            self._wake.set()
