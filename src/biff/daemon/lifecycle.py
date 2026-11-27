"""Install, control and report on biffd's service for this host."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, Self, final

from biff.run_dir import RunDir
from biff.supervision.biffd import BIFFD_SPEC
from biff.supervision.errors import (
    BinaryNotFoundError,
    PortConflictError,
    ServiceActionFailedError,
    ServiceNotInstalledError,
)
from biff.supervision.health import HealthProbe, HealthProbes, HealthUnavailableError
from biff.supervision.host import Detection, ServiceHost
from biff.supervision.manager import ServiceManager
from biff.supervision.port import PortProbe

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["BiffdService", "Outcome"]

_HEALTH_SECONDS: Final = 1.0
NO_MANAGER: Final = (
    "no service manager on this host (systemd --user or launchd); biffd starts "
    "on demand when a client needs it, or run it yourself: biff daemon serve"
)
NOT_REPORTED: Final = "not reported by biffd yet"


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a lifecycle command did, for the command to print and exit on.

    ``no_manager`` is a host with none, such as a container; ``unreachable`` is a
    host whose manager exists and did not answer, with the reason in the message.
    """

    status: Literal["done", "no_manager", "unreachable", "failed"]
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
    def for_host(cls) -> Self:
        """Return the service on this host's service manager."""
        host = ServiceHost.detect(BIFFD_SPEC)
        if host.backend is None:
            return cls(None, host)
        port = PortProbe(BIFFD_SPEC.health_port)
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
        lines.append(f"relay connections: {NOT_REPORTED}")
        lines.append(f"attached clients: {NOT_REPORTED}")
        return Outcome("done", "\n".join(lines))

    def _manager_lines(self) -> list[str]:
        if self._manager is None:
            return [f"service manager: none ({self._absence()})"]
        backend = self._manager.backend
        return [
            f"installed: {'yes' if backend.config_path().exists() else 'no'}",
            f"running: {'yes' if backend.is_active() else 'no'}",
            f"unit: {backend.config_path()}",
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
