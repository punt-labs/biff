"""biffd as a detached process, for hosts with no service manager."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Self, final

from biff.supervision.errors import BinaryNotFoundError
from biff.supervision.runner import Supervisor

if TYPE_CHECKING:
    from biff.supervision.spec import ServiceSpec

__all__ = ["Child", "DetachedDaemon"]


class Child(Protocol):
    """A process that was started; ``subprocess.Popen`` is the real one."""

    def poll(self) -> int | None:
        """Return the exit status, or ``None`` while the process runs."""
        ...


@final
class DetachedDaemon:
    """Starts the service's executable in its own session, as a container does."""

    __slots__ = ("_spec",)

    _spec: ServiceSpec

    def __new__(cls, spec: ServiceSpec) -> Self:
        """Create the launcher for *spec*."""
        self = super().__new__(cls)
        self._spec = spec
        return self

    def binary(self) -> Path | None:
        """Return the executable on ``PATH`` or at its install path.

        ``None`` means biffd is not installed on this host.
        """
        try:
            return self._spec.resolve_binary()
        except BinaryNotFoundError:
            return None

    def launch(self, binary: Path, port: int) -> Child:
        """Start *binary* on *port*, detached from this process and its terminal.

        Raises:
            OSError: The executable cannot be started.
        """
        return Supervisor.spawn_detached([str(binary), "--port", str(port)])
