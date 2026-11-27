"""One service's lifecycle through a backend."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, Protocol, Self, final

from biff.supervision.errors import (
    PortConflictError,
    ServiceActionFailedError,
    ServiceNotInstalledError,
    ServiceStillStartingError,
)
from biff.supervision.evidence import SupervisorEvidence
from biff.supervision.health_wait import HealthWait

if TYPE_CHECKING:
    from biff.supervision.backend import ServiceBackend
    from biff.supervision.holder import Holder
    from biff.supervision.port import PortState
    from biff.supervision.spec import ServiceSpec

__all__ = ["PortWatcher", "ServiceManager"]

logger = logging.getLogger(__name__)


class PortWatcher(Protocol):
    """Watches the service's port; ``PortProbe`` is the real one."""

    def state(self) -> PortState:
        """Return who holds the port."""
        ...

    def wait_until_answering(self, seconds: float) -> bool:
        """Return whether the service answers ``/health`` within *seconds*."""
        ...

    def holder(self) -> Holder | None:
        """Name the program on the port, or return ``None`` when that is not known."""
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
                registered and does not answer ``/health`` within the health wait
                (``BIFF_DAEMON_HEALTH_WAIT``, 30 seconds by default), or that
                variable is not a number of seconds.
        """
        port = self._spec.health_port
        wait = HealthWait.from_environ(os.environ).seconds
        self._refuse_other_holder("install")
        exec_display = " ".join(self._spec.resolve_exec_args())
        up_to_date = self._backend.install()
        evidence = self._evidence()
        if not self._port.wait_until_answering(wait):
            if evidence.is_starting():
                raise ServiceStillStartingError(
                    self._still_starting("install", wait, kept=True)
                )
            # What the supervisor knows must be read before the rollback removes it.
            seen = evidence.describe()
            detail = (
                f"registered, but it does not answer /health on port {port} within "
                f"{wait:g} seconds. {seen} {self._remove_after_failure()} "
                f"To see why, run it in the foreground: {self._spec.cli_name} "
                f"{self._spec.cli_verb} serve"
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

    def _evidence(self) -> SupervisorEvidence:
        """Start the clock and name the log for a service just started."""
        return SupervisorEvidence(self._backend, Path(self._spec.log_path).expanduser())

    def _still_starting(self, action: str, wait: float, *, kept: bool = False) -> str:
        """Say that a running service has not answered yet; it is not a failure."""
        left = "It was left installed. " if kept else ""
        return (
            f"{self._spec.display_name} {action}: still starting after {wait:g} "
            f"seconds (machine load average {os.getloadavg()[0]:.1f}); it is "
            f"running, not crashed. {left}Run: {self._spec.cli_name} "
            f"{self._spec.cli_verb} status"
        )

    def _refuse_other_holder(self, action: str) -> None:
        """Refuse *action* while a program outside the service holds the port.

        A foreground biffd answers ``/health`` too, so without this guard a start
        would report success for a unit whose own biffd cannot bind.

        Raises:
            PortConflictError: Another program, or a biffd the service does not
                supervise, holds the port.
        """
        port = self._spec.health_port
        state = self._port.state()
        lsof = f"lsof -nP -iTCP:{port} -sTCP:LISTEN"
        if state == "foreign":
            holder = self._port.holder()
            by = (
                "another program"
                if holder is None
                else f"{holder.describe()}, not biffd"
            )
            msg = f"port {port} is held by {by}; see what holds it with: {lsof}"
            self._refuse(action, msg)
        if state == "ours" and not self._backend.is_active():
            msg = (
                f"a biffd that this service does not supervise already answers on "
                f"port {port} (perhaps `biff daemon serve`); stop it, then run: "
                f"{self._spec.cli_name} {self._spec.cli_verb} {action}. "
                f"See what holds the port with: {lsof}"
            )
            self._refuse(action, msg)

    def _refuse(self, action: str, msg: str) -> NoReturn:
        """Log the refusal of *action* at WARNING, then raise it.

        Every biff process writes biff.log, so a refusal that is only printed leaves
        nothing behind for the next person to read.

        Raises:
            PortConflictError: Always, with *msg*.
        """
        logger.warning(
            "refused `%s %s %s`: %s",
            self._spec.cli_name,
            self._spec.cli_verb,
            action,
            msg,
        )
        raise PortConflictError(msg)

    def _remove_after_failure(self) -> str:
        """Undo the registration of a service that does not answer; say how it went.

        A service that is registered and dead would be started again at every
        login, so a failed install leaves nothing behind.
        """
        try:
            self._backend.uninstall()
        except (ServiceActionFailedError, OSError, subprocess.TimeoutExpired) as exc:
            return (
                f"Removing it failed too ({exc}); run: {self._spec.cli_name} "
                f"{self._spec.cli_verb} uninstall."
            )
        return "It was removed."

    def uninstall(self) -> str:
        """Remove the service, whether or not it was installed."""
        path = self._backend.config_path()
        self._backend.uninstall()
        return f"{self._spec.display_name} uninstalled. Removed {path}."

    def start(self) -> str:
        """Start the installed service.

        Raises:
            ServiceNotInstalledError: No unit is installed.
            ServiceActionFailedError: The supervisor refused, or the service does
                not answer ``/health`` within the health wait.
        """
        self._require_installed()
        wait = HealthWait.from_environ(os.environ).seconds
        self._refuse_other_holder("start")
        if not self._backend.start():
            raise ServiceActionFailedError(self._spec.failure_message("start"))
        self._require_answering("start", wait)
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
            ServiceActionFailedError: The supervisor refused, or the service does
                not answer ``/health`` within the health wait.
        """
        self._require_installed()
        wait = HealthWait.from_environ(os.environ).seconds
        self._refuse_other_holder("restart")
        if not self._backend.restart():
            raise ServiceActionFailedError(self._spec.failure_message("restart"))
        self._require_answering("restart", wait)
        return f"{self._spec.display_name} restarted."

    def _require_answering(self, action: str, wait: float) -> None:
        """Return once the service answers ``/health``; raise after *wait* seconds."""
        evidence = self._evidence()
        if self._port.wait_until_answering(wait):
            return
        if evidence.is_starting():
            raise ServiceStillStartingError(self._still_starting(action, wait))
        detail = (
            f"the supervisor accepted it, but it does not answer /health on port "
            f"{self._spec.health_port} within {wait:g} seconds. "
            f"{evidence.describe()} Run: {self._spec.cli_name} "
            f"{self._spec.cli_verb} status, then biff doctor"
        )
        raise ServiceActionFailedError(self._spec.failure_message(action, detail))

    def _require_installed(self) -> None:
        if not self._backend.config_path().exists():
            msg = (
                f"{self._spec.display_name} is not installed. "
                f"Run: {self._spec.cli_name} {self._spec.cli_verb} install"
            )
            raise ServiceNotInstalledError(msg)
