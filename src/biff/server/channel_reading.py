"""What the channel emitter reads, and the one reading of a kind."""

from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from biff.server.channel_content import ChannelEvent, EventKind

if TYPE_CHECKING:
    from collections.abc import Mapping

    from biff.relay import Relay
    from biff.talk_state import TalkState


class ArrivalSource(Protocol):
    """What the emitter reads: the relay, the talk state and this session's key."""

    @property
    def relay(self) -> Relay:
        """The relay mail and walls arrive through."""
        ...

    @property
    def talk(self) -> TalkState:
        """The held talk state."""
        ...

    @property
    def session_key(self) -> str:
        """This session's ``user:tty`` key."""
        ...


class EventSink(Protocol):
    """Where an event goes: one connection to a client that takes events.

    The emitter's detection, content, rate limit and last-sent record know
    nothing of the transport; ``ChannelLink`` is the Claude Code one.
    """

    @property
    def is_open(self) -> bool:
        """Whether the connection can still carry an event."""
        ...

    async def send(self, content: str, meta: Mapping[str, str]) -> None:
        """Hand one event to the connection; raise if it cannot take it."""
        ...


class ArrivalReader(Protocol):
    """What the emitter reads kinds from: one reading of a kind at a time."""

    async def reading(self, kind: EventKind) -> Reading:
        """Read *kind* now: its fingerprint, whether it is newer, and its event."""
        ...


@runtime_checkable
class ReconnectNotifier(Protocol):
    """A relay that reports an in-place reconnect to its listeners."""

    def add_reconnect_listener(self, callback: Callable[[], None]) -> None:
        """Call *callback* each time the connection is back."""
        ...


@dataclass(frozen=True, slots=True)
class Reading:
    """What one kind looks like right now, taken before any send.

    *fingerprint* changes when something of that kind arrives.  *commit*
    advances the last-sent record to the value this reading took, which is the
    value read before the send, so an arrival during the send survives it.
    """

    kind: EventKind
    fingerprint: Hashable
    is_newer: bool
    event: ChannelEvent | None
    commit: Callable[[], None]

    @property
    def is_owed(self) -> bool:
        """Whether an event is owed: newer than the record, and still true."""
        return self.is_newer and self.event is not None
