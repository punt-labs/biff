"""An in-memory stand-in for the NATS core publish/subscribe talk rides on."""

from __future__ import annotations

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
        self.relays: list[TalkBusRelay] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        if self.broken:
            msg = "the relay is wedged"
            raise TimeoutError(msg)
        self.published.append((subject, payload))
        for callback in list(self._subscribers.get(subject, [])):
            await callback(BusMessage(payload))

    async def subscribe(self, subject: str, cb: Callback) -> BusSubscription:
        self.subscribes += 1
        self._subscribers.setdefault(subject, []).append(cb)
        return BusSubscription(self, subject, cb)

    def drop(self, subject: str, callback: Callback) -> None:
        self._subscribers[subject].remove(callback)

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
        self.crash: Exception | None = None
        bus.relays.append(self)

    @property
    def connection_generation(self) -> int:
        return self.generation

    def talk_notify_subject(self, session_key: str) -> str:
        return f"talk.notify.{session_key}"

    async def get_nc(self) -> TalkClient:
        if self.crash is not None:
            raise self.crash
        if self.down:
            msg = "no connection"
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
