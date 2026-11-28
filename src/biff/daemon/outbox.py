"""Mail the daemon has fetched for a session and not yet delivered."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    import uuid
    from collections.abc import Callable, Sequence

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

    __slots__ = ("_held", "_retains", "_sink")

    _held: dict[uuid.UUID, Message]
    _retains: bool
    _sink: Callable[[Sequence[Message]], None] | None

    def __new__(cls, *, retains: bool) -> Self:
        """Create an empty outbox; *retains* is whether the relay consumes on fetch."""
        self = super().__new__(cls)
        self._held = {}
        self._retains = retains
        self._sink = None
        return self

    def __len__(self) -> int:
        """Return how many messages are held."""
        return len(self._held)

    def merge(self, fetched: Sequence[Message]) -> list[Message]:
        """Hold *fetched*, replacing a held message its copy came back for; list all."""
        if self._retains and self._sink is not None:
            # Sealed: what is held is in the spill and the hand-back files it back.
            # A fetched message that is already held is not spilled twice; the copy
            # of a held one replaces it (filing the original later would reuse the
            # copy's id, which the relay may drop as a duplicate). The read, whose
            # client is gone, is left nothing.
            fresh: list[Message] = []
            for message in fetched:
                original = message.original_in(self._held)
                if original == message.id and message.id in self._held:
                    continue
                self._held.pop(original, None)
                fresh.append(message)
            self._sink(fresh)
            return []
        if self._retains:
            for message in fetched:
                self._held.pop(message.original_in(self._held), None)
                self._held[message.id] = message
            fetched = list(self._held.values())
        return sorted(fetched, key=lambda m: (m.timestamp, m.id))

    def seal(self, sink: Callable[[Sequence[Message]], None]) -> None:
        """Stop holding: what is fetched from now on goes to *sink*.

        For a daemon that is stopping: it gives back what is held, and a fetch
        that finishes after that would be held in a process about to exit.
        """
        self._sink = sink

    def held(self) -> list[Message]:
        """Return every message held, oldest first."""
        return sorted(self._held.values(), key=lambda m: (m.timestamp, m.id))

    def forget(self, gone: Sequence[Message]) -> None:
        """Forget *gone*: it reached the client, or went back to the relay."""
        for message in gone:
            self._held.pop(message.id, None)
