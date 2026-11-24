"""What the NATS relay delegates: connection diagnostics and wake pokes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, Self

from biff._nats_connection import NatsConnectionSetup
from biff.relay_errors import RelayLog

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class _Publisher(Protocol):
    """The one core-NATS operation a poke needs."""

    async def publish(self, subject: str, payload: bytes) -> None: ...


class NatsSupport(NatsConnectionSetup):
    """Connection diagnostics, plus wake pokes whose failures are logged.

    A poke only wakes another session's poller, so a failure never reaches the
    caller; but with no timer recomputing the unread count, the inbox poke is
    the one thing that tells a sibling session its count fell.  A relay outage
    logs a WARNING at onset, again every ten minutes, and once on recovery; a
    code bug logs at ERROR.
    """

    _pokes: RelayLog

    def __new__(cls) -> Self:
        """Create a setup with no connection or poke failure in progress."""
        self = super().__new__(cls)
        self._pokes = RelayLog(logging.getLogger("biff.nats_relay"))
        return self

    async def poke(
        self,
        connect: Callable[[], Awaitable[_Publisher]],
        subject: Callable[[], str],
        what: str,
    ) -> None:
        """Publish an empty poke on ``subject()`` through the client *connect* gives."""
        try:
            nc = await connect()
            await nc.publish(subject(), b"1")
            self._pokes.recovered(what)
        except Exception as exc:  # noqa: BLE001 — notification is best-effort
            self._pokes.failed(what, exc)
