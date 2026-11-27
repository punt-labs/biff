"""Start biffd the way this host runs services."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol, Self, final

from biff.supervision.detached import DetachedDaemon
from biff.supervision.errors import ServiceActionFailedError
from biff.supervision.host import ServiceHost

if TYPE_CHECKING:
    from pathlib import Path

    from biff.supervision.backend import ServiceBackend
    from biff.supervision.spec import ServiceSpec

__all__ = ["Detacher", "HostStarter", "StartOutcome"]

type StartOutcome = Literal["not_installed", "launched"]


class Detacher(Protocol):
    """Finds and starts a detached process; ``DetachedDaemon`` is the real one."""

    def binary(self) -> Path | None:
        """Return the executable, or ``None`` when it is not installed."""
        ...

    def launch(self, binary: Path, port: int) -> None:
        """Start *binary* on *port* in its own session."""
        ...


@final
class HostStarter:
    """Starts the service through its unit, or detached where there is no manager."""

    __slots__ = ("_backend", "_detacher", "_port", "_spec")

    _spec: ServiceSpec
    _port: int
    # None: the host has no service manager (see ServiceHost.backend).
    _backend: ServiceBackend | None
    _detacher: Detacher

    def __new__(
        cls,
        spec: ServiceSpec,
        port: int,
        backend: ServiceBackend | None,
        detacher: Detacher,
    ) -> Self:
        """Start *spec* on *port* through *backend*, else through *detacher*."""
        self = super().__new__(cls)
        self._spec = spec
        self._port = port
        self._backend = backend
        self._detacher = detacher
        return self

    @classmethod
    def for_host(cls, spec: ServiceSpec, port: int) -> Self:
        """Return the starter for this host's service manager."""
        return cls(spec, port, ServiceHost.backend(spec), DetachedDaemon(spec))

    def start(self) -> StartOutcome:
        """Start biffd, or say it is not installed here.

        With a service manager the unit must exist: a manager without the unit
        is not installed, whatever else is on ``PATH``.  Without one, the
        executable is what "installed" means.

        Raises:
            ServiceActionFailedError: The unit exists but the supervisor refused
                to start it.
        """
        if self._backend is not None:
            if not self._backend.config_path().exists():
                return "not_installed"
            if not self._backend.start():
                raise ServiceActionFailedError(self._spec.failure_message("start"))
            return "launched"
        binary = self._detacher.binary()
        if binary is None:
            return "not_installed"
        self._detacher.launch(binary, self._port)
        return "launched"
