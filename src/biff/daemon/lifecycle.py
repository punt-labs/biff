"""Install, control and report on biffd's service for this host."""

from __future__ import annotations

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
from biff.supervision.health import HealthProbe, HealthUnavailableError
from biff.supervision.host import ServiceHost
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


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a lifecycle command did, for the command to print and exit on."""

    status: Literal["done", "no_manager", "failed"]
    message: str


@final
class BiffdService:
    """biffd's service on this host, or the absence of a service manager."""

    __slots__ = ("_manager",)

    # None: the host has no service manager (see ServiceHost.backend).
    _manager: ServiceManager | None

    def __new__(cls, manager: ServiceManager | None) -> Self:
        """Act through *manager*, or report that the host has none."""
        self = super().__new__(cls)
        self._manager = manager
        return self

    @classmethod
    def for_host(cls) -> Self:
        """Return the service on this host's service manager."""
        backend = ServiceHost.backend(BIFFD_SPEC)
        if backend is None:
            return cls(None)
        port = PortProbe(BIFFD_SPEC.health_port)
        return cls(ServiceManager(BIFFD_SPEC, backend, port.state))

    def install(self) -> Outcome:
        """Register biffd and start it."""
        return self._act(lambda manager: manager.install())

    def uninstall(self) -> Outcome:
        """Remove biffd's service."""
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
        lines = self._manager_lines()
        try:
            port = RunDir.default().read_port()
        except ValueError as exc:
            return Outcome("failed", "\n".join([*lines, f"port: {exc}"]))
        lines.append(f"port: {port}")
        try:
            health = HealthProbe(port).fetch(_HEALTH_SECONDS)
        except HealthUnavailableError as exc:
            lines.append(f"health: not answering ({exc})")
            return Outcome("failed", "\n".join(lines))
        lines.append("health: ok")
        lines.append(f"version: {health.version}")
        lines.append(f"uptime: {health.uptime_seconds:.0f}s")
        return Outcome("done", "\n".join(lines))

    def _manager_lines(self) -> list[str]:
        if self._manager is None:
            return ["service manager: none (a detached biffd serves clients)"]
        backend = self._manager.backend
        return [
            f"installed: {'yes' if backend.config_path().exists() else 'no'}",
            f"running: {'yes' if backend.is_active() else 'no'}",
            f"unit: {backend.config_path()}",
        ]

    def _act(self, action: Callable[[ServiceManager], str]) -> Outcome:
        """Run *action* on the manager, folding a refusal into the outcome."""
        if self._manager is None:
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
