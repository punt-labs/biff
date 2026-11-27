"""Who holds biffd's port."""

from __future__ import annotations

import socket
import time
from typing import TYPE_CHECKING, ClassVar, Final, Literal, Self, final

from biff.supervision.health import HealthProbe, HealthUnavailableError, NotBiffdError
from biff.supervision.holder import PortHolder

if TYPE_CHECKING:
    from collections.abc import Callable

    from biff.supervision.holder import Holder

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

    __slots__ = ("_health", "_notice", "_port")

    # A wait this long with no answer earns one line, so a person does not think
    # a command that waits up to 30 seconds has hung.
    NOTICE_AFTER_SECONDS: ClassVar[float] = 3.0

    _port: int
    _health: HealthProbe
    # None: a caller with no terminal to write to, such as ``biff doctor``.
    _notice: Callable[[str], None] | None

    def __new__(cls, port: int, notice: Callable[[str], None] | None = None) -> Self:
        """Probe loopback *port*; *notice* hears the one line about a long wait."""
        self = super().__new__(cls)
        self._port = port
        self._health = HealthProbe(port)
        self._notice = notice
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

    def holder(self) -> Holder | None:
        """Name the program on the port when the host can say; ``None`` when not."""
        return PortHolder.find(self._port)

    def wait_until_answering(self, seconds: float) -> bool:
        """Return whether biffd answers ``/health`` within *seconds*."""
        begun = time.monotonic()
        told = False
        while not self._health.answers(_TIMEOUT_SECONDS):
            if time.monotonic() - begun >= seconds:
                return False
            told = told or self._tell_if_long(begun, seconds)
            time.sleep(_POLL_SECONDS)
        return True

    def _tell_if_long(self, begun: float, seconds: float) -> bool:
        """Send the notice once the wait is long; return whether it was sent."""
        if self._notice is None or time.monotonic() - begun < self.NOTICE_AFTER_SECONDS:
            return False
        self._notice(
            f"still waiting for biffd to answer /health on port {self._port} "
            f"(giving up after {seconds:g} seconds)"
        )
        return True
