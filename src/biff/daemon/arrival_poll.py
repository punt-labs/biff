"""The pass that finds what arrived for a session, on a push or on the clock."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from biff.daemon.arrivals_reading import ArrivalsReading
from biff.daemon.refusal import ClientGoneError
from biff.relay_errors import RELAY_ERRORS, RelayLog

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pydantic import JsonValue

    from biff.daemon.sessions import Session
    from biff.frames import EventKind
    from biff.models import WallPost

__all__ = ["ArrivalPoll"]

logger = logging.getLogger(__name__)

_WHAT = "subscription check"


@dataclass(frozen=True, slots=True)
class _Reading:
    """What one read of the relay found."""

    mark: int
    wall: WallPost | None
    count: int | None  # the unread count, read only when the mark rose


@final
class ArrivalPoll:
    """Reads a session's mail mark and wall and announces what rose.

    There is one pass, run when a push of an arrival notice wakes it and every
    *interval* seconds whether or not one came (``PushWake`` and ``PollWake`` of
    ``notification-mail-event.tex``): a push is at least once and may be lost, a
    wake is a flag so a repeat changes nothing, and the pass reads every kind, so
    no kind waits on another.  It marks no mail read.

    A client that has left ends the poll quietly: nothing is announced to a
    connection that is gone, and an arrival whose notice was not delivered is not
    counted as announced.

    A relay outage is not fatal: the pass is tried again, the client is told once
    that it is ``degraded`` and once that it ``recovered``, and one log line is
    written per episode rather than one per pass.
    """

    __slots__ = (
        "_degraded",
        "_emit",
        "_interval",
        "_log",
        "_reading",
        "_session",
        "_summarized",
        "_wake",
    )

    _session: Session
    _emit: Callable[[EventKind, JsonValue], Awaitable[None]]
    _interval: float
    _wake: asyncio.Event
    _reading: ArrivalsReading
    _log: RelayLog
    _degraded: bool
    _summarized: asyncio.Event

    def __new__(
        cls,
        session: Session,
        emit: Callable[[EventKind, JsonValue], Awaitable[None]],
        interval: float,
        wake: asyncio.Event,
    ) -> Self:
        """Poll *session* every *interval* seconds, or when *wake* is set."""
        self = super().__new__(cls)
        self._session = session
        self._emit = emit
        self._interval = interval
        self._wake = wake
        self._reading = ArrivalsReading(session)
        self._log = RelayLog(logger)
        self._degraded = False
        self._summarized = asyncio.Event()
        return self

    @property
    def summarized(self) -> asyncio.Event:
        """Set once the starting summary, and the wall if any, has been sent."""
        return self._summarized

    async def run(self) -> None:
        """Send the current summary, then each arrival, until the client leaves."""
        try:
            await self._announce_forever()
        except ClientGoneError:
            logger.debug("client left; the arrival poll ends")

    async def _announce_forever(self) -> None:
        mark, wall, count = await self._baseline()
        await self._emit("unread", {"count": count})
        if wall is not None:
            await self._emit("wall", wall.model_dump(mode="json"))
        self._summarized.set()
        while True:
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self._interval)
            self._wake.clear()
            try:
                reading = await self._read(mark)
            except RELAY_ERRORS as exc:
                await self._outage(exc)
                continue
            await self._recovered()
            mark, wall = await self._announce(reading, mark, wall)

    async def _baseline(self) -> tuple[int, WallPost | None, int]:
        """Read the starting point, retrying through an outage."""
        relay = self._session.context.relay
        while True:
            try:
                mark = await self._reading.mark()
                wall = await relay.get_wall()
                count = await self._reading.unread()
            except RELAY_ERRORS as exc:
                await self._outage(exc)
                await asyncio.sleep(self._interval)
            else:
                await self._recovered()
                return mark, wall, count

    async def _read(self, mark: int) -> _Reading:
        """Read the relay: the mark, the wall, and the count if the mark rose."""
        current = await self._reading.mark()
        posted = await self._session.context.relay.get_wall()
        # A read can lower the mark; only a rise is an arrival.
        count = await self._reading.unread() if current > mark else None
        return _Reading(current, posted, count)

    async def _announce(
        self, reading: _Reading, mark: int, wall: WallPost | None
    ) -> tuple[int, WallPost | None]:
        """Emit what *reading* finds new; advance each baseline once it was sent.

        A send that fails ends the poll with the error, so an arrival whose
        notice was not delivered is never counted as announced.  A send is the
        client's business, not the relay's: it is never reported as an outage.
        """
        if reading.count is not None:
            await self._emit("mail", {"count": reading.count, "mark": reading.mark})
        mark = reading.mark
        if reading.wall is not None and reading.wall != wall:
            await self._emit("wall", reading.wall.model_dump(mode="json"))
        return mark, reading.wall

    async def _outage(self, exc: Exception) -> None:
        self._log.failed(_WHAT, exc)
        if not self._degraded:
            self._degraded = True
            await self._emit("degraded", {"reason": type(exc).__name__})

    async def _recovered(self) -> None:
        self._log.recovered(_WHAT)
        if self._degraded:
            self._degraded = False
            await self._emit("recovered", {})
