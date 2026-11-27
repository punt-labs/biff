"""Start biffd when a client finds it down, and say why when it cannot.

A caller runs ``DaemonNudge.for_host(run_dir).ensure_running()`` before it
connects, and connects to the port that returns.  The call is blocking.
``for_host`` asks the service manager whether it answers; ``ensure_running``
spends one second to see that biffd is down, then the supervisor's answer to
"start", then five seconds waiting for ``/health``.  That is about six seconds
when the supervisor answers at once.  A hung ``systemctl`` is cut off after 30
seconds, and two such calls (discovery and start) bound the worst case at 66.  A
caller with a tighter limit, such as a hook, runs both in a thread it can
abandon: ``await asyncio.to_thread(...)``.  Every failure is a
``ConnectionError``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final, Protocol, Self, final

from biff.supervision.biffd import BIFFD_SPEC
from biff.supervision.errors import ServiceActionFailedError
from biff.supervision.health import HealthProbes
from biff.supervision.starter import HostStarter

if TYPE_CHECKING:
    from biff.run_dir import RunDir
    from biff.supervision.spec import ServiceSpec
    from biff.supervision.starter import StartOutcome

__all__ = [
    "DaemonDidNotStartError",
    "DaemonNotInstalledError",
    "DaemonNudge",
    "DaemonPortFileError",
    "DaemonStartFailedError",
]

_CONNECT_SECONDS: Final = 1.0
_PATIENCE_SECONDS: Final = 5.0
_POLL_SECONDS: Final = 0.1
_NOT_INSTALLED: Final = "biffd is not installed. Run: biff install"
_DID_NOT_START: Final = (
    "biffd is installed but did not start within 5 seconds. "
    "Run: biff daemon status, then biff doctor"
)
_DIAGNOSE: Final = "Run: biff daemon status, then biff doctor"


class DaemonNotInstalledError(ConnectionError):
    """Neither a unit nor a ``biffd`` executable exists on this host."""


class DaemonDidNotStartError(ConnectionError):
    """biffd is installed, was started, and still does not answer ``/health``."""


class DaemonStartFailedError(ConnectionError):
    """biffd is installed and starting it failed, with a reason in the message.

    The supervisor refused, did not answer, or is missing, or the detached
    process could not be launched or exited at once.
    """


class DaemonPortFileError(ConnectionError):
    """The record of biffd's port is unreadable, so no client can find biffd."""


class Probe(Protocol):
    """Asks where biffd answers; ``HealthProbes`` is the real one."""

    def live_port(self, timeout: float) -> int | None:
        """Return the port where ``/health`` answers within *timeout*, or ``None``.

        ``None`` is the absence the nudge reports, not a failure.
        """
        ...


class Starter(Protocol):
    """Starts biffd on this host; ``HostStarter`` is the real one."""

    def start(self) -> StartOutcome:
        """Start biffd, or report that it is not installed."""
        ...

    def exit_note(self) -> str:
        """Say how a started process ended, or return ``""`` while it runs."""
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
    def for_host(cls, run_dir: RunDir, spec: ServiceSpec = BIFFD_SPEC) -> Self:
        """Return the nudge for this host's service manager.

        The recorded port in *run_dir* is tried first, so a ``--port`` override
        reaches every client, then the port *spec* starts biffd on, so a stale
        record left by a crash does not hide a healthy daemon.

        Raises:
            DaemonPortFileError: The port file does not hold a port number.
        """
        try:
            recorded = run_dir.read_port()
        except ValueError as exc:
            msg = f"{exc}; remove {run_dir.port_path} and run: biff daemon start"
            raise DaemonPortFileError(msg) from exc
        except OSError as exc:
            msg = f"cannot read {run_dir.port_path}: {exc}"
            raise DaemonPortFileError(msg) from exc
        return cls(HealthProbes(recorded, spec.health_port), HostStarter.for_host(spec))

    def ensure_running(self) -> int:
        """Return the port biffd answers on, starting biffd if it does not.

        The port is the one that answered ``/health``: it can differ from the
        recorded one, which a crash on a ``--port`` override leaves stale.

        Raises:
            DaemonNotInstalledError: There is nothing to start.
            DaemonStartFailedError: Starting biffd failed, with the reason.
            DaemonDidNotStartError: biffd was started and did not answer in time.
        """
        if (port := self._probe.live_port(_CONNECT_SECONDS)) is not None:
            return port
        try:
            outcome = self._starter.start()
        except ServiceActionFailedError as exc:
            raise DaemonStartFailedError(f"{exc} {_DIAGNOSE}") from exc
        if outcome == "not_installed":
            raise DaemonNotInstalledError(_NOT_INSTALLED)
        deadline = time.monotonic() + self._patience
        while time.monotonic() < deadline:
            if (port := self._probe.live_port(_CONNECT_SECONDS)) is not None:
                return port
            if note := self._starter.exit_note():
                raise DaemonStartFailedError(f"{note}. {_DIAGNOSE}")
            time.sleep(self._interval)
        raise DaemonDidNotStartError(_DID_NOT_START)
