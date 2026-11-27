"""One service's lifecycle through a backend."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Self, final

from biff.supervision.errors import (
    PortConflictError,
    ServiceActionFailedError,
    ServiceNotInstalledError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from biff.supervision.backend import ServiceBackend
    from biff.supervision.port import PortState
    from biff.supervision.spec import ServiceSpec

__all__ = ["ServiceManager"]


@final
class ServiceManager:
    """Installs, removes, starts and stops one service; refuses a taken port."""

    __slots__ = ("_backend", "_port_state", "_spec")

    _spec: ServiceSpec
    _backend: ServiceBackend
    _port_state: Callable[[], PortState]

    def __new__(
        cls,
        spec: ServiceSpec,
        backend: ServiceBackend,
        port_state: Callable[[], PortState],
    ) -> Self:
        """Manage *spec* through *backend*; *port_state* says who holds its port."""
        self = super().__new__(cls)
        self._spec = spec
        self._backend = backend
        self._port_state = port_state
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
        """Register and start the service, and describe the result.

        Raises:
            PortConflictError: A program other than biffd holds the port.
            BinaryNotFoundError: The executable is not installed.
        """
        port = self._spec.health_port
        if self._port_state() == "foreign":
            msg = (
                f"port {port} is held by another program; see what holds it "
                f"with: lsof -nP -iTCP:{port} -sTCP:LISTEN"
            )
            raise PortConflictError(msg)
        exec_display = " ".join(self._spec.resolve_exec_args())
        up_to_date = self._backend.install()
        if up_to_date:
            label = "already installed and up to date"
        elif self._backend.is_active():
            label = "running"
        else:
            label = "installed (not yet running)"
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
