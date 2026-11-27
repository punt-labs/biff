"""Start biffd when a client finds it down, and say why when it cannot.

A caller runs ``DaemonNudge.for_host(run_dir).ensure_running()`` before it
connects.  The call is blocking and takes at most about six seconds: one to see
that biffd is down and five to wait for it.  From async code, run it with
``await asyncio.to_thread(nudge.ensure_running)``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final, Protocol, Self, final

from biff.supervision.biffd import BIFFD_SPEC
from biff.supervision.errors import ServiceActionFailedError
from biff.supervision.health import HealthProbe
from biff.supervision.starter import HostStarter

if TYPE_CHECKING:
    from biff.run_dir import RunDir
    from biff.supervision.starter import StartOutcome

__all__ = ["DaemonDidNotStartError", "DaemonNotInstalledError", "DaemonNudge"]

_CONNECT_SECONDS: Final = 1.0
_PATIENCE_SECONDS: Final = 5.0
_POLL_SECONDS: Final = 0.1


_NOT_INSTALLED: Final = "biffd is not installed. Run: biff install"
_DID_NOT_START: Final = (
    "biffd is installed but did not start within 5 seconds. "
    "Run: biff daemon status, then biff doctor"
)


class DaemonNotInstalledError(ConnectionError):
    """Neither a unit nor a ``biffd`` executable exists on this host."""


class DaemonDidNotStartError(ConnectionError):
    """biffd is installed, was started, and still does not answer ``/health``."""


class Probe(Protocol):
    """Asks whether biffd answers; ``HealthProbe`` is the real one."""

    def answers(self, timeout: float) -> bool:
        """Return whether ``/health`` answers within *timeout* seconds."""
        ...


class Starter(Protocol):
    """Starts biffd on this host; ``HostStarter`` is the real one."""

    def start(self) -> StartOutcome:
        """Start biffd, or report that it is not installed."""
        ...


@final
class DaemonNudge:
    """Brings biffd up for a client that found it down."""

    __slots__ = ("_interval", "_patience", "_probe", "_starter")

    _probe: Probe
    _starter: Starter
    _patience: float
    _interval: float

    def __new__(
        cls,
        probe: Probe,
        starter: Starter,
        *,
        patience: float = _PATIENCE_SECONDS,
        interval: float = _POLL_SECONDS,
    ) -> Self:
        """Wait up to *patience* seconds, polling every *interval*, after a start."""
        self = super().__new__(cls)
        self._probe = probe
        self._starter = starter
        self._patience = patience
        self._interval = interval
        return self

    @classmethod
    def for_host(cls, run_dir: RunDir) -> Self:
        """Return the nudge for this host's service manager.

        The port is the one a running daemon recorded in *run_dir*, else 8424, so
        a ``--port`` override reaches every client.

        Raises:
            ValueError: The port file does not hold a port number.
        """
        port = run_dir.read_port()
        return cls(HealthProbe(port), HostStarter.for_host(BIFFD_SPEC, port))

    def ensure_running(self) -> None:
        """Return once biffd answers ``/health``, starting it if it does not.

        Raises:
            DaemonNotInstalledError: There is nothing to start.
            DaemonDidNotStartError: biffd was started and did not answer in time,
                or its supervisor refused to start it.
        """
        if self._probe.answers(_CONNECT_SECONDS):
            return
        try:
            outcome = self._starter.start()
        except ServiceActionFailedError as exc:
            raise DaemonDidNotStartError(_DID_NOT_START) from exc
        if outcome == "not_installed":
            raise DaemonNotInstalledError(_NOT_INSTALLED)
        deadline = time.monotonic() + self._patience
        while time.monotonic() < deadline:
            if self._probe.answers(_CONNECT_SECONDS):
                return
            time.sleep(self._interval)
        raise DaemonDidNotStartError(_DID_NOT_START)
