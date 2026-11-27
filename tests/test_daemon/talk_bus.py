"""An in-memory stand-in for the NATS core publish/subscribe talk rides on."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from biff.relay import LocalRelay

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from biff.daemon.talk_feed import TalkClient
    from biff.models import UserSession


@dataclass(frozen=True)
class BusMessage:
    """What a subscriber's callback receives."""

    data: bytes


type Callback = Callable[[BusMessage], Awaitable[None]]


@final
class BusSubscription:
    """The handle ``subscribe`` returns."""

    def __init__(self, bus: TalkBus, subject: str, callback: Callback) -> None:
        self._bus = bus
        self._subject = subject
        self._callback = callback

    async def unsubscribe(self) -> None:
        self._bus.drop(self._subject, self._callback)


@final
class TalkBus:
    """Core NATS in miniature: a subject reaches every current subscriber once."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callback]] = {}
        self.published: list[tuple[str, bytes]] = []
        self.subscribes = 0
        self.broken = False
        self.subscribe_delay = 0.0
        self.subscribe_block: asyncio.Event | None = None
        self.flush_gate: asyncio.Event | None = None
        self.flush_fails = False
        self.failed_flushes: Counter[str] = Counter()  # per subject
        self._queued: list[tuple[str, Callback]] = []
        self.relays: list[TalkBusRelay] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        if self.broken:
            msg = "the relay is wedged"
            raise TimeoutError(msg)
        self.published.append((subject, payload))
        for callback in list(self._subscribers.get(subject, [])):
            await callback(BusMessage(payload))

    async def subscribe(self, subject: str, cb: Callback) -> BusSubscription:
        await asyncio.sleep(self.subscribe_delay)
        if self.subscribe_block is not None:
            await self.subscribe_block.wait()
        self.subscribes += 1
        # As with NATS, the server acts on the subscription only once the client
        # has flushed it: until then a frame for the subject is lost.
        self._queued.append((subject, cb))
        return BusSubscription(self, subject, cb)

    async def flush(self) -> None:
        if self.flush_fails:
            for subject, _ in self._queued:  # each feed's own subscription
                self.failed_flushes[subject] += 1
            msg = "the flush timed out"
            raise TimeoutError(msg)
        if self.flush_gate is not None:
            await self.flush_gate.wait()
        queued, self._queued = self._queued, []
        for subject, cb in queued:
            self._subscribers.setdefault(subject, []).append(cb)

    def drop(self, subject: str, callback: Callback) -> None:
        if (subject, callback) in self._queued:
            self._queued.remove((subject, callback))
        else:
            self._subscribers[subject].remove(callback)

    def total_subscribers(self) -> int:
        return sum(len(callbacks) for callbacks in self._subscribers.values())

    def subscriber_count(self, subject: str) -> int:
        return len(self._subscribers.get(subject, []))


@final
class TalkBusRelay(LocalRelay):
    """A local relay whose talk frames travel over a shared :class:`TalkBus`."""

    def __init__(self, data_dir: Path, bus: TalkBus) -> None:
        super().__init__(data_dir)
        self._bus = bus
        self.generation = 1
        self.down = False
        self.stale = False
        self.failed_dials: Counter[str] = Counter()  # per session key
        self.crash: Exception | None = None
        self.dial_fails = False
        self.failed_acquisitions = 0
        bus.relays.append(self)

    @property
    def connection_generation(self) -> int:
        return self.generation

    def talk_notify_subject(self, session_key: str) -> str:
        # The feed asks for its subject right after it dials, so this is where
        # an outage is felt by one session's feed and counted for that session.
        if self.crash is not None:
            self.failed_dials[session_key] += 1
            raise self.crash
        if self.down:
            self.failed_dials[session_key] += 1
            msg = "no connection"
            raise TimeoutError(msg)
        return f"talk.notify.{session_key}"

    async def get_nc(self) -> TalkClient:
        if self.dial_fails:  # acquiring the connection itself fails
            self.failed_acquisitions += 1
            msg = "cannot acquire a connection"
            raise TimeoutError(msg)
        return self._bus

    async def get_session(self, session_key: str) -> UserSession | None:
        self._check_presence()
        return await super().get_session(session_key)

    async def update_session(self, session: UserSession) -> None:
        self._check_presence()
        await super().update_session(session)

    def _check_presence(self) -> None:
        if self.stale:
            msg = "presence is unreachable"
            raise TimeoutError(msg)
