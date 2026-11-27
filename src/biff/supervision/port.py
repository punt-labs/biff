"""Who holds biffd's port."""

from __future__ import annotations

import socket
import time
from typing import Final, Literal, Self, final

from biff.supervision.health import HealthProbe, HealthUnavailableError, NotBiffdError

__all__ = ["PortProbe", "PortState"]

# free: nothing listens.  ours: biffd answers.  foreign: something answers and it
# is not biffd.  unresponsive: a listener accepted the connection and said
# nothing within a second, which biffd under load also does.
type PortState = Literal["free", "ours", "foreign", "unresponsive"]

_TIMEOUT_SECONDS: Final = 1.0
_POLL_SECONDS: Final = 0.2


@final
class PortProbe:
    """Tells a free port from biffd's own and from another program's."""

    __slots__ = ("_health", "_port")

    _port: int
    _health: HealthProbe

    def __new__(cls, port: int) -> Self:
        """Probe loopback *port*."""
        self = super().__new__(cls)
        self._port = port
        self._health = HealthProbe(port)
        return self

    def state(self) -> PortState:
        """Return what holds the port."""
        try:
            socket.create_connection(
                ("127.0.0.1", self._port), timeout=_TIMEOUT_SECONDS
            ).close()
        except OSError:
            return "free"
        try:
            self._health.fetch(_TIMEOUT_SECONDS)
        except NotBiffdError:
            return "foreign"
        except HealthUnavailableError:
            return "unresponsive"
        return "ours"

    def wait_until_answering(self, seconds: float) -> bool:
        """Return whether biffd answers ``/health`` within *seconds*."""
        deadline = time.monotonic() + seconds
        while True:
            if self._health.answers(_TIMEOUT_SECONDS):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_POLL_SECONDS)
