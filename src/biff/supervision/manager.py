"""One service's lifecycle through a backend."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final, Protocol, Self, final

from biff.supervision.errors import (
    PortConflictError,
    ServiceActionFailedError,
    ServiceNotInstalledError,
)

if TYPE_CHECKING:
    from biff.supervision.backend import ServiceBackend
    from biff.supervision.port import PortState
    from biff.supervision.spec import ServiceSpec

__all__ = ["PortWatcher", "ServiceManager"]

_HEALTH_WAIT_SECONDS: Final = 5.0


class PortWatcher(Protocol):
    """Watches the service's port; ``PortProbe`` is the real one."""

    def state(self) -> PortState:
        """Return who holds the port."""
        ...

    def wait_until_answering(self, seconds: float) -> bool:
        """Return whether the service answers ``/health`` within *seconds*."""
        ...


@final
class ServiceManager:
    """Installs, removes, starts and stops one service; refuses a taken port."""

    __slots__ = ("_backend", "_port", "_spec")

    _spec: ServiceSpec
    _backend: ServiceBackend
    _port: PortWatcher

    def __new__(
        cls, spec: ServiceSpec, backend: ServiceBackend, port: PortWatcher
    ) -> Self:
        """Manage *spec* through *backend*; *port* watches its health port."""
        self = super().__new__(cls)
        self._spec = spec
        self._backend = backend
        self._port = port
        return self

    @property
    def spec(self) -> ServiceSpec:
        """The service this manager acts on."""
        return self._spec

    @property
    def backend(self) -> ServiceBackend:
        """The platform backend the manager drives."""
        return self._backend

    def install(self) -> str:
        """Register and start the service, prove it answers, and describe the result.

        Raises:
            PortConflictError: A program other than biffd holds the port.
            BinaryNotFoundError: The executable is not installed.
            ServiceActionFailedError: The supervisor refused, or the service was
                registered and does not answer ``/health`` within five seconds.
        """
        port = self._spec.health_port
        if self._port.state() == "foreign":
            msg = (
                f"port {port} is held by another program; see what holds it "
                f"with: lsof -nP -iTCP:{port} -sTCP:LISTEN"
            )
            raise PortConflictError(msg)
        exec_display = " ".join(self._spec.resolve_exec_args())
        up_to_date = self._backend.install()
        if not self._port.wait_until_answering(_HEALTH_WAIT_SECONDS):
            detail = (
                f"registered, but it does not answer /health on port {port} within "
                f"{_HEALTH_WAIT_SECONDS:g} seconds. Run: {self._spec.cli_name} "
                f"{self._spec.cli_verb} status, then {self._spec.cli_name} doctor"
            )
            raise ServiceActionFailedError(
                self._spec.failure_message("install", detail)
            )
        label = "already installed and up to date" if up_to_date else "running"
        lines = [
            f"{self._spec.display_name} {label}.",
            f"  Service: {self._backend.config_path()}",
            f"  Command: {exec_display}",
        ]
        if warning := self._backend.linger_warning():
            lines.append(warning)
        return os.linesep.join(lines)

    def uninstall(self) -> str:
        """Remove the service, whether or not it was installed."""
        path = self._backend.config_path()
        self._backend.uninstall()
        return f"{self._spec.display_name} uninstalled. Removed {path}."

    def start(self) -> str:
        """Start the installed service.

        Raises:
            ServiceNotInstalledError: No unit is installed.
            ServiceActionFailedError: The supervisor refused.
        """
        self._require_installed()
        if not self._backend.start():
            raise ServiceActionFailedError(self._spec.failure_message("start"))
        return f"{self._spec.display_name} started."

    def stop(self) -> str:
        """Stop the service, leaving it installed.

        Raises:
            ServiceActionFailedError: The supervisor refused.
        """
        if not self._backend.stop():
            raise ServiceActionFailedError(self._spec.failure_message("stop"))
        return f"{self._spec.display_name} stopped."

    def restart(self) -> str:
        """Restart the installed service under its supervisor.

        Raises:
            ServiceNotInstalledError: No unit is installed.
            ServiceActionFailedError: The supervisor refused.
        """
        self._require_installed()
        if not self._backend.restart():
            raise ServiceActionFailedError(self._spec.failure_message("restart"))
        return f"{self._spec.display_name} restarted."

    def _require_installed(self) -> None:
        if not self._backend.config_path().exists():
            msg = (
                f"{self._spec.display_name} is not installed. "
                f"Run: {self._spec.cli_name} {self._spec.cli_verb} install"
            )
            raise ServiceNotInstalledError(msg)
