"""A service's identity under each supervisor, and its command line."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import final

from biff.supervision.errors import BinaryNotFoundError

__all__ = ["ServiceSpec"]


@final
@dataclass(frozen=True, slots=True)
class ServiceSpec:
    """One service's launchd and systemd names and its executable command.

    The fields are those of ``punt_service.ServiceSpec`` that biffd uses, plus
    ``permanent_exit_status``, so the spec moves to that library with one field
    added there.
    """

    display_name: str
    launchd_label: str
    # The unit name without the ``.service`` suffix, as punt-service spells it.
    systemd_unit: str
    systemd_description: str
    binary_name: str
    extra_args: tuple[str, ...]
    cli_verb: str
    cli_name: str
    process_name: str
    health_port: int
    # A clean exit is the operator's stop; only a crash is restarted.
    restart_on_crash_only: bool
    log_path: str
    # The exit status of a failure no restart can cure, such as a port that is
    # taken or a run directory that is not private.  A supervisor that is told
    # it must not restart on it does not loop.
    permanent_exit_status: int

    def failure_message(self, action: str, detail: str = "") -> str:
        """Describe a refused supervisor *action*, with *detail*, and name the log."""
        why = f": {detail}" if detail else "."
        return (
            f"{self.display_name} {action} failed{why} See {self.log_path} for details."
        )

    def resolve_exec_args(self) -> list[str]:
        """Return the command line, finding the executable as an install put it.

        ``uv tool install`` links it into ``~/.local/bin``; pipx and Homebrew put
        it elsewhere on ``PATH``.

        Raises:
            BinaryNotFoundError: The executable is in neither place.
        """
        binary = self.resolve_binary()
        return [str(binary), *self.extra_args]

    def resolve_binary(self) -> Path:
        """Return the executable at ``~/.local/bin``, else the one on ``PATH``.

        Raises:
            BinaryNotFoundError: The executable is in neither place.
        """
        installed = self.binary_path
        if installed.exists():
            return installed
        found = shutil.which(self.binary_name)
        if found is None:
            msg = (
                f"Cannot find {self.binary_name} binary at {installed} or on PATH. "
                "Install it first: uv tool install punt-biff"
            )
            raise BinaryNotFoundError(msg)
        return Path(found)

    @property
    def binary_path(self) -> Path:
        """Where ``uv tool install`` puts the executable."""
        return Path.home() / ".local" / "bin" / self.binary_name
