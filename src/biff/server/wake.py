"""The signal that wakes the poller when something arrives.

The poller sleeps until a relay push (a NATS callback or KV watcher) or a
local-relay file change calls :meth:`WakeSignal.wake`.  Nothing schedules a
wake on a timer.

Asyncio is single-threaded, so no locking is needed.  Callers on another
thread must go through ``loop.call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
from contextlib import AbstractContextManager, nullcontext
from enum import Enum, auto
from typing import TYPE_CHECKING, Self

from biff.local_watch import LocalWatch
from biff.relay import LocalRelay

if TYPE_CHECKING:
    from biff.relay import Relay


class WakeOutcome(Enum):
    """Why :meth:`WakeSignal.wait` returned — the caller's next action differs."""

    SHUTDOWN = auto()  # shutdown fired — the poller must exit
    EVENT = auto()  # the signal fired before the timeout — an arrival
    TIMEOUT = auto()  # neither fired — the liveness interval simply elapsed

    @property
    def is_shutdown(self) -> bool:
        """Whether the poller must exit."""
        return self is WakeOutcome.SHUTDOWN

    @property
    def is_arrival(self) -> bool:
        """Whether something woke the signal, as opposed to a liveness timeout."""
        return self is WakeOutcome.EVENT


class WakeSignal:
    """A level-triggered wake flag shared by the poller and its wakers.

    Companion to the frozen ``ServerState``.  :meth:`wake` is safe to call
    at any time and from any number of wakers: wakes that land while the
    poller is busy coalesce into one, so the poller runs once more after it
    finishes rather than once per arrival.
    """

    __slots__ = ("_event",)

    _event: asyncio.Event

    def __new__(cls) -> Self:
        """Create an unset signal."""
        self = super().__new__(cls)
        self._event = asyncio.Event()
        return self

    @property
    def is_set(self) -> bool:
        """Whether a wake is waiting to be consumed."""
        return self._event.is_set()

    def wake(self) -> None:
        """Ask the poller to run its next pass now."""
        self._event.set()

    def watching(self, relay: Relay) -> AbstractContextManager[object]:
        """Return what wakes this signal when *relay* has an arrival.

        The local relay has no push, so a watch on its data directory is how an
        arrival reaches the poller without a timer (DES-087).  Enter it from
        the event loop.  A NATS-backed relay is woken by its subscriptions
        instead, so there is nothing to watch.
        """
        if isinstance(relay, LocalRelay):
            return LocalWatch(relay.data_dir, self.wake)
        return nullcontext()

    async def wait(
        self, *, timeout: float | None, shutdown: asyncio.Event | None
    ) -> WakeOutcome:
        """Sleep until a wake or *shutdown*, or *timeout* if one is given.

        Distinguishing :attr:`WakeOutcome.EVENT` from
        :attr:`WakeOutcome.TIMEOUT` lets the caller tell "there is an arrival
        to react to" apart from "nothing happened, this was just the liveness
        check".  Consumes the wake on every return so one this call already
        reported cannot immediately re-trigger the next.

        *timeout* is ``None`` for a poller with nothing to keep alive: a
        filesystem relay holds no subscription, so it sleeps until its file
        watch wakes it.  A NATS-backed poller passes its liveness interval: a
        client discard (a wedge teardown's ``_force_reconnect``, or
        ``_on_closed`` giving up) strands every always-on SUB on the dead
        client, and rebinding them is the poller's own job, which runs only
        after this wait returns.  With no live SUB, no poke can ever arrive to
        end the wait, so the very thing that would fix the stranding never
        gets to run — the ``nats-relay.tex`` liveness proof (a stranded SUB
        always eventually re-binds) assumes *some* scheduler keeps calling
        ``Subscribe``; this timeout keeps that assumption true.  It is
        connection liveness, not a cadence for finding arrivals (DES-087).
        """
        waiters = [asyncio.ensure_future(self._event.wait())]
        if shutdown is not None:
            waiters.append(asyncio.ensure_future(shutdown.wait()))
        try:
            done, _pending = await asyncio.wait(
                waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in waiters:
                if not task.done():
                    task.cancel()
        # Snapshot the flag before clearing it, rather than trusting `done`
        # alone. asyncio.wait()'s timeout resolution and a callback's wake()
        # are two independent events that can land in either order in the same
        # narrow window; a wake() that wins that race without getting the
        # wait-task into `done` would otherwise be erased by the clear() below
        # with no outcome reflecting it, and the arrival would be lost until
        # the next wake, not just delayed.  Only this method clears the flag,
        # so nothing else can flip it between this read and the clear.
        woke = self._event.is_set()
        self._event.clear()
        if shutdown is not None and shutdown.is_set():
            return WakeOutcome.SHUTDOWN
        if done or woke:
            return WakeOutcome.EVENT
        return WakeOutcome.TIMEOUT
