"""Install, control and report on biffd's service for this host."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, Self, final

from biff.run_dir import RunDir
from biff.supervision.biffd import BIFFD_SPEC
from biff.supervision.diagnosable import LoadReporting
from biff.supervision.errors import (
    BinaryNotFoundError,
    PortConflictError,
    ServiceActionFailedError,
    ServiceNotInstalledError,
    ServiceStillStartingError,
)
from biff.supervision.health import HealthProbe, HealthProbes, HealthUnavailableError
from biff.supervision.host import Detection, ServiceHost
from biff.supervision.manager import ServiceManager
from biff.supervision.port import PortProbe

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["NO_MANAGER_EXIT", "STARTING_EXIT", "BiffdService", "Outcome"]

_HEALTH_SECONDS: Final = 1.0
NO_MANAGER: Final = (
    "no service manager on this host (systemd --user or launchd); biffd is not "
    "supervised here, so start it yourself: biff daemon serve"
)
NO_MANAGER_EXIT: Final = 3
"""Exit status of a service command on a host with no usable manager, apart from
1, a manager that refused: a bootstrap treats the first as a note, the second as
a failure."""
STARTING_EXIT: Final = 4
"""Exit status of a service command whose biffd is running and has not answered
yet.  It is not 3: a bootstrap that reads 3 as "no manager" would tell the user to
start a foreground biffd that competes with the one the service is starting."""
NOT_REPORTED: Final = "not reported by biffd yet"


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a lifecycle command did, for the command to print and exit on.

    ``no_manager`` is a host with none, such as a container; ``unreachable`` is a
    host whose manager exists and did not answer, with the reason in the message;
    ``starting`` is a service that is running and has not answered ``/health`` yet.
    """

    status: Literal["done", "no_manager", "unreachable", "failed", "starting"]
    message: str


@final
class BiffdService:
    """biffd's service on this host, or the absence of a usable service manager."""

    __slots__ = ("_host", "_manager")

    # None: no usable service manager; ``_host`` says whether it is absent or
    # unreachable.
    _manager: ServiceManager | None
    _host: Detection

    def __new__(cls, manager: ServiceManager | None, host: Detection) -> Self:
        """Act through *manager*, or report what *host* found instead."""
        self = super().__new__(cls)
        self._manager = manager
        self._host = host
        return self

    @classmethod
    def for_host(cls, notice: Callable[[str], None]) -> Self:
        """Return the service on this host's service manager.

        *notice* hears the one line a long wait for ``/health`` earns, such as
        ``typer.echo`` writing to stderr.
        """
        host = ServiceHost.detect(BIFFD_SPEC)
        if host.backend is None:
            return cls(None, host)
        port = PortProbe(BIFFD_SPEC.health_port, notice)
        return cls(ServiceManager(BIFFD_SPEC, host.backend, port), host)

    def install(self) -> Outcome:
        """Register biffd, start it, and prove it answers."""
        return self._act(lambda manager: manager.install())

    def uninstall(self) -> Outcome:
        """Remove biffd's service.

        Without a usable manager, a unit or plist an earlier login registered is
        still there and would start biffd again at the next login; that is a
        failure with the command to run where the manager answers.
        """
        if self._manager is None:
            left = ServiceHost.leftover_files(BIFFD_SPEC)
            if left:
                why = self._host.reason or "there is no service manager here"
                return Outcome(
                    "failed",
                    f"biffd's service file {left[0]} is still installed and cannot "
                    f"be removed from here ({why}). Run, from a session that "
                    f"reaches the service manager: biff daemon uninstall",
                )
        return self._act(lambda manager: manager.uninstall())

    def start(self) -> Outcome:
        """Start the installed service."""
        return self._act(lambda manager: manager.start())

    def stop(self) -> Outcome:
        """Stop the service, leaving it installed."""
        return self._act(lambda manager: manager.stop())

    def restart(self) -> Outcome:
        """Restart the installed service."""
        return self._act(lambda manager: manager.restart())

    def status(self) -> Outcome:
        """Report whether the service is installed and running, and biffd's health.

        ``done`` means biffd answers ``/health``; ``failed`` means it does not.
        """
        try:
            lines = self._manager_lines()
            recorded = RunDir.default().read_port()
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            return Outcome("failed", f"status could not be read: {exc}")
        probes = HealthProbes(recorded, BIFFD_SPEC.health_port)
        port = probes.live_port(_HEALTH_SECONDS) or recorded
        lines.append(f"port: {port}")
        try:
            health = HealthProbe(port).fetch(_HEALTH_SECONDS)
        except HealthUnavailableError as exc:
            lines.append(f"health: not answering ({exc})")
            return Outcome("failed", "\n".join(lines))
        lines.append("health: ok")
        lines.append(f"version: {health.version}")
        lines.append(f"uptime: {health.uptime_seconds:.0f}s")
        if health.pid is not None:
            lines.append(f"pid: {health.pid}")
        lines.append(f"relay connections: {NOT_REPORTED}")
        lines.append(f"attached clients: {NOT_REPORTED}")
        return Outcome("done", "\n".join(lines))

    def _manager_lines(self) -> list[str]:
        if self._manager is None:
            return [f"service manager: none ({self._absence()})"]
        backend = self._manager.backend
        installed = backend.config_path().exists()
        running = backend.is_active()
        # launchd's stop boots the job out and keeps the plist: installed, not loaded.
        # A running job is loaded, so only a job that is not running is asked: the
        # lines then come from one answer and cannot disagree.
        unloaded = (
            installed
            and not running
            and isinstance(backend, LoadReporting)
            and not backend.is_loaded()
        )
        note = " (installed, not loaded)" if unloaded else ""
        return [
            f"installed: {'yes' if installed else 'no'}",
            f"running: {'yes' if running else 'no'}",
            f"unit: {backend.config_path()}{note}",
        ]

    def _absence(self) -> str:
        """Say why there is no manager: the reason, or that a detached biffd serves."""
        return self._host.reason or "a detached biffd serves clients"

    def _act(self, action: Callable[[ServiceManager], str]) -> Outcome:
        """Run *action* on the manager, folding any failure into the outcome."""
        if self._manager is None:
            if self._host.status == "unreachable":
                return Outcome("unreachable", f"{NO_MANAGER} [{self._host.reason}]")
            return Outcome("no_manager", NO_MANAGER)
        try:
            return Outcome("done", action(self._manager))
        except ServiceStillStartingError as exc:
            return Outcome("starting", str(exc))
        except (
            BinaryNotFoundError,
            PortConflictError,
            ServiceActionFailedError,
            ServiceNotInstalledError,
        ) as exc:
            return Outcome("failed", str(exc))
        except subprocess.TimeoutExpired as exc:
            return Outcome("failed", f"{exc.cmd} timed out after {exc.timeout:g}s")
        except OSError as exc:
            return Outcome("failed", f"the service manager could not be used: {exc}")
