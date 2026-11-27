"""The arrival subscription: arrival summaries written as event frames."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self, final

from biff.frames import Event
from biff.relay_errors import RELAY_ERRORS, RelayLog

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from pydantic import JsonValue

    from biff.cli_context import CliContext
    from biff.daemon.sessions import Session
    from biff.frames import EventKind
    from biff.models import WallPost

__all__ = ["Subscription"]

logger = logging.getLogger(__name__)

_WHAT = "subscription check"


@final
class Subscription:
    """Tells one connection what reaches its session.

    On start it sends the session's current unread summary and, when the
    repository has an active wall, the wall.  Then it sends one event per later
    arrival.  An event carries counts, the wall, or who a talk frame came from,
    never a message body or a line of talk.
    There is no replay log: a client that reconnects starts from the summary,
    which counts the mail the daemon holds for the session as well as the
    relay's: a consuming relay no longer lists mail the daemon fetched and could
    not deliver, and the client must be told it waits.

    A relay outage is not fatal: the subscription retries, tells the client once
    that it is ``degraded`` and once that it ``recovered``, and writes one log
    line per episode rather than one per poll.  Anything that is not a relay
    error is a bug and ends the task, which the connection reports.
    """

    __slots__ = ("_degraded", "_interval", "_log", "_send", "_seq", "_session")

    _session: Session
    _send: Callable[[Event], Awaitable[None]]
    _seq: Iterator[int]
    _interval: float
    _log: RelayLog
    _degraded: bool

    def __new__(
        cls,
        session: Session,
        send: Callable[[Event], Awaitable[None]],
        seq: Iterator[int],
        interval: float,
    ) -> Self:
        """Create a subscription for *session* that writes events with *send*.

        *seq* is the daemon-wide counter that orders events; *interval* is how
        often the relay is checked for arrivals.
        """
        self = super().__new__(cls)
        self._session = session
        self._send = send
        self._seq = seq
        self._interval = interval
        self._log = RelayLog(logger)
        self._degraded = False
        return self

    @property
    def _ctx(self) -> CliContext:
        """The session's current context: its org scope can change while it runs."""
        return self._session.context

    async def run(self) -> None:
        """Send the current summary, then each arrival, until cancelled.

        Mail and wall arrivals are found by polling the relay; talk arrivals are
        pushed by the session's talk feed, so a conversation is not paced by the
        poll interval.
        """
        async with asyncio.TaskGroup() as group:
            group.create_task(self._poll())
            group.create_task(self._session.talk.announce(self._emit))

    async def _poll(self) -> None:
        """Send the current summary, then each mail and wall arrival."""
        mark, wall, count = await self._baseline()
        await self._emit("unread", {"count": count})
        if wall is not None:
            await self._emit("wall", self._wall_data(wall))
        while True:
            await asyncio.sleep(self._interval)
            try:
                mark, wall = await self._arrivals(mark, wall)
            except RELAY_ERRORS as exc:
                await self._outage(exc)
            else:
                await self._recovered()

    async def _baseline(self) -> tuple[int, WallPost | None, int]:
        """Read the starting point, retrying through an outage."""
        relay = self._ctx.relay
        while True:
            try:
                mark = await relay.inbox_high_water(self._ctx.session_key)
                wall = await relay.get_wall()
                count = await self._unread()
            except RELAY_ERRORS as exc:
                await self._outage(exc)
                await asyncio.sleep(self._interval)
            else:
                await self._recovered()
                return mark, wall, count

    async def _arrivals(
        self, mark: int, wall: WallPost | None
    ) -> tuple[int, WallPost | None]:
        """Emit what arrived since *mark* and *wall*; return the new baseline."""
        relay = self._ctx.relay
        current = await relay.inbox_high_water(self._ctx.session_key)
        posted = await relay.get_wall()
        # A read can lower the mark; only a rise is an arrival.
        if current > mark:
            await self._emit("mail", {"count": await self._unread()})
        if posted is not None and posted != wall:
            await self._emit("wall", self._wall_data(posted))
        return current, posted

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

    async def _unread(self) -> int:
        """Return the unread count: the relay's, plus the mail the daemon holds."""
        ctx = self._ctx
        summary = await ctx.relay.get_unread_summary(ctx.session_key)
        return summary.count + len(self._session.outbox)

    async def _emit(self, kind: EventKind, data: JsonValue) -> None:
        await self._send(Event(seq=next(self._seq), kind=kind, data=data))

    @staticmethod
    def _wall_data(wall: WallPost) -> JsonValue:
        return wall.model_dump(mode="json")
