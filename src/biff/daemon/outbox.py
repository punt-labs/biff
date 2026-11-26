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
    """Holds fetched mail until a reply carrying it has been sent.

    Some relays consume mail when it is fetched (the NATS relay acks on fetch and
    a work queue deletes on ack).  Once fetched, such mail exists only here, so a
    reply that is lost, or a batch that leaves part of the inbox for the next
    read, must not lose it.  A relay that does not consume returns the same mail
    again on the next fetch; messages are told apart by id, so it is held once.

    Mail the daemon holds is lost if the daemon dies first, as mail a client had
    fetched is lost if the client dies: delivery is at least once, never silent.
    """

    __slots__ = ("_held",)

    _held: dict[uuid.UUID, Message]

    def __new__(cls) -> Self:
        """Create an empty outbox."""
        self = super().__new__(cls)
        self._held = {}
        return self

    def __len__(self) -> int:
        """Return how many messages are held."""
        return len(self._held)

    def merge(self, fetched: Sequence[Message]) -> list[Message]:
        """Hold *fetched* alongside what is held; return all of it, oldest first."""
        for message in fetched:
            self._held.setdefault(message.id, message)
        return sorted(self._held.values(), key=lambda m: m.timestamp)

    def delivered(self, delivered: Sequence[Message]) -> None:
        """Forget *delivered*: it reached the client."""
        for message in delivered:
            self._held.pop(message.id, None)
