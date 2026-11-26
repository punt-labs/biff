"""Replies whose mail waits for the client to say it received them."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from biff.daemon.methods import Acknowledge

__all__ = ["Acknowledgements"]

logger = logging.getLogger(__name__)

_MAX_PENDING = 256
"""The most unconfirmed replies one connection holds before the oldest is dropped.

A client that abandons reads without acking would otherwise accumulate their held
snapshots until it disconnects.  A dropped reply's mail stays unread (or held in
the outbox), served again by a later read: delivery is at least once.
"""


@final
class Acknowledgements:
    """What one connection has promised to do once its client confirms a reply.

    A socket write says the bytes left, not that the client read them: a client
    that timed out discards a late reply.  So a reply that carries mail is
    registered here, and the mail is marked read only when the client's ack
    frame names the request.  A connection that ends first leaves its mail
    unread (or held in the session's outbox), to be served again.
    """

    __slots__ = ("_pending",)

    _pending: dict[int, Acknowledge]

    def __new__(cls) -> Self:
        """Create a connection's empty set of unconfirmed replies."""
        self = super().__new__(cls)
        self._pending = {}
        return self

    def __len__(self) -> int:
        """Return how many replies still wait for their client's ack."""
        return len(self._pending)

    def expect(self, request_id: int, acknowledge: Acknowledge) -> None:
        """Run *acknowledge* when the client confirms the reply to *request_id*.

        Once the connection holds :data:`_MAX_PENDING` unconfirmed replies, the
        oldest is dropped so an abandoning client cannot grow this without bound;
        its mail stays unread, to be served again.
        """
        while len(self._pending) >= _MAX_PENDING and request_id not in self._pending:
            oldest = next(iter(self._pending))
            del self._pending[oldest]
            logger.warning(
                "dropped unconfirmed reply %d; its mail stays unread", oldest
            )
        self._pending[request_id] = acknowledge

    def forget(self, request_id: int) -> None:
        """Drop the promise for *request_id*: its reply never left."""
        self._pending.pop(request_id, None)

    async def confirm(self, request_id: int) -> None:
        """Run the acknowledgement of *request_id*, once; an unknown id is logged."""
        acknowledge = self._pending.pop(request_id, None)
        if acknowledge is None:
            logger.warning("ack for request %d, which waits for none", request_id)
            return
        await acknowledge()
