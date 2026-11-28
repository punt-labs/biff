"""The arrival subscription: arrival summaries written as event frames."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Self, final

from biff.daemon.arrival_poll import ArrivalPoll
from biff.frames import Event

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from pydantic import JsonValue

    from biff.daemon.sessions import Session
    from biff.frames import EventKind

__all__ = ["Subscription"]


@final
class Subscription:
    """Tells one connection what reaches its session.

    On start it sends the session's current unread summary and, when the
    repository has an active wall, the wall.  Then it sends one event per later
    arrival.  An event carries counts, the mark, the wall, or who a talk frame
    came from, never a message body or a line of talk.
    There is no replay log: a client that reconnects starts from the summary,
    which counts the mail the daemon holds for the session as well as the
    relay's: a consuming relay no longer lists mail the daemon fetched and could
    not deliver, and the client must be told it waits.

    Anything that is not a relay error is a bug and ends the task, which the
    connection reports.
    """

    __slots__ = ("_interval", "_send", "_seq", "_session")

    _session: Session
    _send: Callable[[Event], Awaitable[None]]
    _seq: Iterator[int]
    _interval: float

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
        return self

    async def run(self) -> None:
        """Send the current summary, then each arrival, until cancelled.

        Mail and wall arrivals are found by one pass, run on the interval and
        whenever a push wakes it; talk arrivals are pushed by the session's talk
        feed, so a conversation is not paced by the interval.
        """
        push = self._session.push
        with push.wakes.listen() as wake, push.checks.listen() as checks:
            poll = ArrivalPoll(self._session, self._emit, self._interval, wake)
            async with asyncio.TaskGroup() as group:
                group.create_task(poll.run())
                group.create_task(self._session.talk.announce(self._emit))
                group.create_task(self._announce_checks(checks, poll.summarized))

    async def _announce_checks(
        self, tokens: asyncio.Queue[str], summarized: asyncio.Event
    ) -> None:
        """Send each channel check token, the pending one first, after the summary."""
        await summarized.wait()  # the summary comes first on every subscription
        while True:
            await self._emit("channel_check_request", {"token": await tokens.get()})

    async def _emit(self, kind: EventKind, data: JsonValue) -> None:
        await self._send(Event(seq=next(self._seq), kind=kind, data=data))
