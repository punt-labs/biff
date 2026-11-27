"""The Linux backend: a systemd user unit."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from biff.supervision.errors import ServiceActionFailedError
from biff.supervision.runner import Supervisor

if TYPE_CHECKING:
    from biff.supervision.spec import ServiceSpec

__all__ = ["SystemdBackend"]


@final
class SystemdBackend:
    """Runs one service as a unit in ``~/.config/systemd/user``; needs no sudo."""

    __slots__ = ("_spec", "_unit", "_unit_path")

    _spec: ServiceSpec
    _unit: str
    _unit_path: Path

    def __new__(cls, spec: ServiceSpec) -> Self:
        """Create the backend for *spec*."""
        self = super().__new__(cls)
        self._spec = spec
        self._unit = spec.systemd_unit
        self._unit_path = (
            Path.home() / ".config" / "systemd" / "user" / f"{self._unit}.service"
        )
        return self

    @staticmethod
    def available() -> bool:
        """Return whether a systemd user manager answers on this host."""
        try:
            return (
                Supervisor.run("systemctl", "--user", "show-environment").returncode
                == 0
            )
        except FileNotFoundError:
            return False

    def config_path(self) -> Path:
        """Return the unit file path."""
        return self._unit_path

    def unit_text(self) -> str:
        """Return the unit file for the service."""
        exec_start = " ".join(map(self._quote, self._spec.resolve_exec_args()))
        restart = "on-failure" if self._spec.restart_on_crash_only else "always"
        return textwrap.dedent(f"""\
            [Unit]
            Description={self._spec.systemd_description}
            After=network.target

            [Service]
            ExecStart={exec_start}
            Restart={restart}
            RestartSec=5

            [Install]
            WantedBy=default.target
        """)

    def install(self) -> bool:
        """Write the unit, reload systemd, then enable and restart the service.

        Returns:
            False: installing always restarts, so it is never a no-op.
        """
        self._unit_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_atomically(self.unit_text())
        self._require("daemon-reload")
        self._require("enable", "--now", self._unit)
        self._require("restart", self._unit)
        return False

    def uninstall(self) -> None:
        """Stop, disable and remove the unit; a missing unit is not an error."""
        if not self._unit_path.exists():
            return
        Supervisor.run("systemctl", "--user", "disable", "--now", self._unit)
        self._unit_path.unlink()
        Supervisor.run("systemctl", "--user", "daemon-reload")

    def enable_autostart(self) -> bool:
        """Enable the unit for the next login without starting it."""
        return Supervisor.succeeds("systemctl", "--user", "enable", self._unit)

    def start(self) -> bool:
        """Start the installed unit."""
        return Supervisor.succeeds("systemctl", "--user", "start", self._unit)

    def stop(self) -> bool:
        """Stop the unit; it stays enabled."""
        return Supervisor.succeeds("systemctl", "--user", "stop", self._unit)

    def restart(self) -> bool:
        """Restart the unit in one supervisor call."""
        return Supervisor.succeeds("systemctl", "--user", "restart", self._unit)

    def is_active(self) -> bool:
        """Return whether systemd reports the unit active."""
        result = Supervisor.run("systemctl", "--user", "is-active", self._unit)
        return result.stdout.strip() == "active"

    def linger_warning(self) -> str:
        """Warn that the unit stops at logout unless linger is enabled."""
        try:
            user = os.getlogin()
            shown = Supervisor.run(
                "loginctl", "show-user", user, "--property=Linger"
            ).stdout
        except (OSError, FileNotFoundError):
            return ""  # no login session or no loginctl: nothing to warn about
        if "Linger=yes" in shown:
            return ""
        return (
            "  Warning: loginctl linger is not enabled. biffd stops when you "
            "log out. Run: loginctl enable-linger"
        )

    def _require(self, *args: str) -> None:
        """Run ``systemctl --user`` *args*; raise when it fails."""
        result = Supervisor.run("systemctl", "--user", *args)
        if result.returncode != 0:
            msg = f"systemctl --user {' '.join(args)} failed: {result.stderr.strip()}"
            raise ServiceActionFailedError(msg)

    def _write_atomically(self, text: str) -> None:
        """Replace the unit file whole, so systemd never reads half of it."""
        temp = self._unit_path.with_name(f"{self._unit_path.name}.{os.getpid()}.tmp")
        temp.write_text(text)
        temp.replace(self._unit_path)

    @staticmethod
    def _quote(arg: str) -> str:
        """Quote *arg* for ExecStart, which has its own parser and is not a shell."""
        escaped = arg.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
