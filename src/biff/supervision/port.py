"""Who holds biffd's port."""

from __future__ import annotations

import socket
from typing import Final, Literal, Self, final

from biff.supervision.health import HealthProbe

__all__ = ["PortProbe", "PortState"]

type PortState = Literal["free", "ours", "foreign"]

_TIMEOUT_SECONDS: Final = 1.0


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
        """Return ``free``, ``ours`` (biffd answers) or ``foreign``."""
        try:
            socket.create_connection(
                ("127.0.0.1", self._port), timeout=_TIMEOUT_SECONDS
            ).close()
        except OSError:
            return "free"
        return "ours" if self._health.answers(_TIMEOUT_SECONDS) else "foreign"
