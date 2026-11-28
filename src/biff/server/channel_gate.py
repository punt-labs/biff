"""When an event may go out: connected, initialised and mesg on (DES-087)."""

from __future__ import annotations

import asyncio
import logging
from enum import Enum, auto
from typing import Protocol, Self

from biff.relay_errors import RelayLog
from biff.server.channel_reading import ArrivalSource

# One log channel for everything the emitter says, whichever class writes it.
logger = logging.getLogger("biff.server.channel_emitter")


class Gate(Enum):
    """Whether an event may go out now."""

    OPEN = auto()
    CLOSED = auto()  # not initialised, or mesg off: stays owed, nothing to retry
    UNREADABLE = auto()  # the mesg record could not be read: try again


class ReadinessGate(Protocol):
    """What the emitter asks before it sends: ready and allowed, or not."""

    def initializing(self) -> None:
        """Close the gate: the client sent ``initialize`` and is not yet ready."""
        ...

    def initialized(self) -> None:
        """Open the gate to the client: it sent ``notifications/initialized``."""
        ...

    async def state(self) -> Gate:
        """Say whether an event may go out now."""
        ...


class SendGate:
    """The client's readiness and the user's mesg, read when an event is due.

    Mesg is read from the session record (``/mesg n`` writes it), so the answer
    is current whichever tool turned it off.  A record that cannot be read is
    neither on nor off: the caller retries, and the failure is logged, because
    an event that ignored mesg could not be taken back.
    """

    _source: ArrivalSource
    _is_initialized: bool
    _log: RelayLog
    _noted: str

    def __new__(cls, source: ArrivalSource) -> Self:
        """Create a gate that is closed until the client says it is initialised."""
        self = super().__new__(cls)
        self._source = source
        self._is_initialized = False
        self._log = RelayLog(logger)
        self._noted = ""
        return self

    def initializing(self) -> None:
        """Close the gate: the client sent ``initialize`` and is not yet ready."""
        self._is_initialized = False

    def initialized(self) -> None:
        """Open the gate to the client: it sent ``notifications/initialized``."""
        self._is_initialized = True

    async def state(self) -> Gate:
        """Say whether an event may go out; INFO-log each change of that."""
        if not self._is_initialized:
            self._note("the client is not initialised")
            return Gate.CLOSED
        source = self._source
        try:
            session = await source.relay.get_session(source.session_key)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("channel mesg read failed", exc_info=True)
            self._log.failed("channel mesg read", exc)
            return Gate.UNREADABLE
        self._log.recovered("channel mesg read")
        is_off = session is not None and not session.biff_enabled
        self._note("mesg is off" if is_off else "")
        return Gate.CLOSED if is_off else Gate.OPEN

    def _note(self, reason: str) -> None:
        """Log at INFO, once per change, that events are held back; "" if not."""
        if reason != self._noted:
            self._noted = reason
            logger.info("channel events %s", f"held: {reason}" if reason else "flowing")
