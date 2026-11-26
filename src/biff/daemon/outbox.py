"""Mail the daemon has fetched for a session and not yet delivered."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

    from biff.models import Message

__all__ = ["Outbox"]


@final
class Outbox:
    """Holds fetched mail until the client confirmed it received it.

    Some relays consume mail when it is fetched (the NATS relay acks on fetch and
    a work queue deletes on ack).  Once fetched, such mail exists only here, so a
    reply that is lost, or a batch that leaves part of the inbox for the next
    read, must not lose it.

    A relay that does not consume keeps unread mail itself, and its inbox can be
    shared: another session may mark a message read at any time.  Holding that
    mail here would serve it again after it was read, so such an outbox holds
    nothing and each read is built from what the relay says is unread now.

    Mail the daemon holds is lost if the daemon dies first, as mail a client had
    fetched is lost if the client dies: delivery is at least once, never silent.
    """

    __slots__ = ("_held", "_retains")

    _held: dict[uuid.UUID, Message]
    _retains: bool

    def __new__(cls, *, retains: bool) -> Self:
        """Create an empty outbox; *retains* is whether the relay consumes on fetch."""
        self = super().__new__(cls)
        self._held = {}
        self._retains = retains
        return self

    def __len__(self) -> int:
        """Return how many messages are held."""
        return len(self._held)

    def merge(self, fetched: Sequence[Message]) -> list[Message]:
        """Hold *fetched* alongside what is held; return all of it, oldest first."""
        if not self._retains:
            return sorted(fetched, key=lambda m: m.timestamp)
        for message in fetched:
            self._held.setdefault(message.id, message)
        return sorted(self._held.values(), key=lambda m: m.timestamp)

    def delivered(self, delivered: Sequence[Message]) -> None:
        """Forget *delivered*: it reached the client."""
        for message in delivered:
            self._held.pop(message.id, None)
