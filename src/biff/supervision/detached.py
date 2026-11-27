"""biffd as a detached process, for hosts with no service manager."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from biff.supervision.runner import Supervisor

if TYPE_CHECKING:
    from biff.supervision.spec import ServiceSpec

__all__ = ["DetachedDaemon"]


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
        found = shutil.which(self._spec.binary_name)
        if found is not None:
            return Path(found)
        installed = self._spec.binary_path
        return installed if installed.exists() else None

    def launch(self, binary: Path, port: int) -> None:
        """Start *binary* on *port*, detached from this process and its terminal."""
        Supervisor.spawn_detached([str(binary), "--port", str(port)])
