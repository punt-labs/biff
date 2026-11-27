"""A service's identity under each supervisor, and its command line."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import final

from biff.supervision.errors import BinaryNotFoundError

__all__ = ["ServiceSpec"]


@final
@dataclass(frozen=True, slots=True)
class ServiceSpec:
    """One service's launchd and systemd names and its executable command.

    The fields are those of ``punt_service.ServiceSpec`` that biffd uses, so the
    spec moves to that library unchanged when it is published.
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

    def failure_message(self, action: str) -> str:
        """Describe a refused supervisor *action* and name the log."""
        return f"{self.display_name} {action} failed. See {self.log_path} for details."

    def resolve_exec_args(self) -> list[str]:
        """Return the command line, resolving the stable install symlink.

        Raises:
            BinaryNotFoundError: The executable is not in ``~/.local/bin``.
        """
        binary = self.binary_path
        if not binary.exists():
            msg = (
                f"Cannot find {self.binary_name} binary at {binary}. "
                f"Install it first: uv tool install punt-biff"
            )
            raise BinaryNotFoundError(msg)
        return [str(binary), *self.extra_args]

    @property
    def binary_path(self) -> Path:
        """Where ``uv tool install`` puts the executable."""
        return Path.home() / ".local" / "bin" / self.binary_name
