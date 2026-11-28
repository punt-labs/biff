"""What holds a session while it works: mail moves, requests run, an end waits."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Self, final

from biff.daemon.refusal import RefusalError
from biff.frame_reply import ErrorCode

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

__all__ = ["SessionHold"]


@final
class SessionHold:
    """The two holds on one session.

    ``exclusive`` serializes mail moving in or out of its outbox.  ``requests``
    counts the requests running, so an end can wait for those it found and refuse
    new ones: an end deletes the session's row, and a request that was running
    would write it back.
    """

    __slots__ = ("_busy", "_count", "_ending", "_idle", "_queued")

    _busy: asyncio.Lock
    _queued: int
    _count: int
    _idle: asyncio.Event
    _ending: bool

    def __new__(cls) -> Self:
        """Start with nothing held and no end begun."""
        self = super().__new__(cls)
        self._busy = asyncio.Lock()
        self._queued = 0
        self._count = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._ending = False
        return self

    @property
    def queued(self) -> int:
        """How many callers hold :meth:`exclusive` or wait for it.

        Nothing in the daemon reads it: a test waits for it to reach a number to
        know a caller is parked at the hold, instead of guessing how long that takes.
        """
        return self._queued

    @asynccontextmanager
    async def exclusive(self) -> AsyncGenerator[None]:
        """Hold the session while mail moves in or out of its outbox.

        A read holds it from the fetch to the merge into the outbox, and ending
        or expiring the session holds it while it forwards the outbox and drains
        the relay, so neither can run between the other's steps.
        """
        self._queued += 1
        try:
            async with self._busy:
                yield
        finally:
            self._queued -= 1

    @asynccontextmanager
    async def requests(self, *, counted: bool, refuse: bool) -> AsyncGenerator[None]:
        """Hold the session for one request; *refuse* it when the session is over.

        Raises:
            RefusalError: *refuse*, or an end has begun.
        """
        if not counted:
            yield
            return
        if refuse or self._ending:
            msg = "session ended; start a new session"
            raise RefusalError.of(ErrorCode.SESSION_ENDED, msg)
        self._count += 1
        self._idle.clear()
        try:
            yield
        finally:
            self._count -= 1
            if self._count == 0:
                self._idle.set()

    async def begin_end(self) -> None:
        """Refuse new requests, and wait for those running to finish."""
        self._ending = True
        await self._idle.wait()

    def abort_end(self) -> None:
        """Accept requests again: the end failed, and the session goes on."""
        self._ending = False
