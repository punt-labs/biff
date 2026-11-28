"""The Linux backend: a systemd user unit."""

from __future__ import annotations

import os
import subprocess
import textwrap
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from biff.supervision.errors import ServiceActionFailedError
from biff.supervision.runner import Supervisor

if TYPE_CHECKING:
    from biff.supervision.spec import ServiceSpec

__all__ = ["SystemdBackend"]

_START_LIMIT_SECONDS = 60
_START_LIMIT_BURST = 5
# How long systemd waits for biffd's stop before it kills it: above the daemon's
# own stop budget, below the limit of the supervisor call that waits for it.
_STOP_TIMEOUT_SECONDS = 45


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
        config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        self._unit_path = (
            Path(config_home) / "systemd" / "user" / f"{self._unit}.service"
        )
        return self

    @staticmethod
    def unavailable_reason() -> str:
        """Return why no systemd user manager answers here, or ``""`` when one does."""
        try:
            probe = Supervisor.run("systemctl", "--user", "show-environment")
        except FileNotFoundError:
            return "systemctl is not installed"
        except subprocess.TimeoutExpired:
            return "`systemctl --user show-environment` timed out"
        except OSError as exc:
            return f"cannot run systemctl: {exc}"
        if probe.returncode == 0:
            return ""
        said = probe.stderr.strip() or f"exited {probe.returncode}"
        return f"`systemctl --user show-environment` failed: {said}"

    @staticmethod
    def available() -> bool:
        """Return whether a systemd user manager answers on this host."""
        return not SystemdBackend.unavailable_reason()

    def config_path(self) -> Path:
        """Return the unit file path."""
        return self._unit_path

    def unit_text(self) -> str:
        """Return the unit file for the service."""
        # The ":" prefix turns off $VARIABLE substitution, so a "$" in the path or in
        # an argument stays literal; "%" specifiers are still expanded, so _quote
        # doubles them.
        exec_start = ":" + " ".join(map(self._quote, self._spec.resolve_exec_args()))
        restart = "on-failure" if self._spec.restart_on_crash_only else "always"
        return textwrap.dedent(f"""\
            [Unit]
            Description={self._spec.systemd_description}
            After=network.target
            StartLimitIntervalSec={_START_LIMIT_SECONDS}
            StartLimitBurst={_START_LIMIT_BURST}

            [Service]
            ExecStart={exec_start}
            Restart={restart}
            RestartSec=5
            TimeoutStopSec={_STOP_TIMEOUT_SECONDS}
            RestartPreventExitStatus={self._spec.permanent_exit_status}

            [Install]
            WantedBy=default.target
        """)

    def install(self) -> bool:
        """Write the unit, reload systemd, then enable and restart the service.

        A step that fails leaves nothing behind: the unit is disabled and removed,
        so a later check never finds half an install.

        Returns:
            False: installing always restarts, so it is never a no-op.

        Raises:
            ServiceActionFailedError: A systemctl step failed; the error carries
                its stderr.
        """
        self._unit_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_atomically(self.unit_text())
        try:
            self._require("install", "daemon-reload")
            self._require("install", "enable", "--now", self._unit)
            self._require(
                "install",
                "restart",
                self._unit,
                timeout=Supervisor.STOP_TIMEOUT_SECONDS,
            )
        except (ServiceActionFailedError, OSError, subprocess.TimeoutExpired):
            self._roll_back()
            raise
        return False

    def uninstall(self) -> None:
        """Stop, disable and remove the unit; nothing to remove is not an error.

        The unit file is deleted only after systemd confirms the service is
        stopped and disabled, so a failure never leaves a running service with no
        unit to manage it.

        Raises:
            ServiceActionFailedError: systemctl refused, or the service is still
                active; the error carries its stderr.
        """
        existed = self._unit_path.exists()
        if existed:
            self._require(
                "uninstall",
                "disable",
                "--now",
                self._unit,
                timeout=Supervisor.STOP_TIMEOUT_SECONDS,
            )
        elif self.is_active():
            self._require(
                "uninstall", "stop", self._unit, timeout=Supervisor.STOP_TIMEOUT_SECONDS
            )
        if self.is_active():
            msg = self._spec.failure_message(
                "uninstall", f"{self._unit} is still active after it was stopped"
            )
            raise ServiceActionFailedError(msg)
        if existed:
            self._unit_path.unlink()
            self._require("uninstall", "daemon-reload")

    def enable_autostart(self) -> bool:
        """Enable the unit for the next login without starting it."""
        return Supervisor.succeeds("systemctl", "--user", "enable", self._unit)

    def start(self) -> bool:
        """Start the installed unit.

        Raises:
            ServiceActionFailedError: systemctl refused; the error carries its stderr.
        """
        self._require("start", "start", self._unit)
        return True

    def stop(self) -> bool:
        """Stop the unit; it stays enabled.

        Raises:
            ServiceActionFailedError: systemctl refused; the error carries its stderr.
        """
        self._require(
            "stop", "stop", self._unit, timeout=Supervisor.STOP_TIMEOUT_SECONDS
        )
        return True

    def restart(self) -> bool:
        """Restart the unit in one supervisor call.

        Raises:
            ServiceActionFailedError: systemctl refused; the error carries its stderr.
        """
        self._require(
            "restart", "restart", self._unit, timeout=Supervisor.STOP_TIMEOUT_SECONDS
        )
        return True

    def is_active(self) -> bool:
        """Return whether systemd reports the unit active."""
        result = Supervisor.run("systemctl", "--user", "is-active", self._unit)
        return result.stdout.strip() == "active"

    def diagnosis(self) -> str:
        """Return the first lines of ``systemctl --user status`` for the unit."""
        return Supervisor.excerpt(
            "systemctl",
            "--user",
            "status",
            "--no-pager",
            self._unit,
        )

    def main_pid(self) -> int | None:
        """Return the unit's ``MainPID``, or ``None`` when systemd shows 0."""
        result = Supervisor.run(
            "systemctl", "--user", "show", "-p", "MainPID", "--value", self._unit
        )
        pid = result.stdout.strip()
        return int(pid) if pid.isdecimal() and int(pid) > 0 else None

    def linger_warning(self) -> str:
        """Warn that the unit stops at logout unless linger is enabled.

        A loginctl that is missing, hung or failing leaves linger unknown, and an
        unknown state earns no warning.
        """
        try:
            user = os.getlogin()
            shown = Supervisor.run("loginctl", "show-user", user, "--property=Linger")
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if shown.returncode != 0 or "Linger=yes" in shown.stdout:
            return ""
        return (
            "  Warning: loginctl linger is not enabled. biffd stops when you "
            "log out. Run: loginctl enable-linger"
        )

    def _require(
        self,
        action: str,
        *args: str,
        timeout: float | None = None,  # None: the default limit
    ) -> None:
        """Run ``systemctl --user`` *args*; raise with its stderr when it fails.

        A command that waits for biffd to stop is given ``STOP_TIMEOUT_SECONDS``.
        """
        Supervisor.require(
            self._spec, action, "systemctl", "--user", *args, timeout=timeout
        )

    def _roll_back(self) -> None:
        """Undo a partial install; each step is best effort and the unit always goes."""
        with suppress(OSError, subprocess.TimeoutExpired):
            Supervisor.run(
                "systemctl",
                "--user",
                "disable",
                "--now",
                self._unit,
                timeout=Supervisor.STOP_TIMEOUT_SECONDS,
            )
        self._unit_path.unlink(missing_ok=True)
        with suppress(OSError, subprocess.TimeoutExpired):
            Supervisor.run("systemctl", "--user", "daemon-reload")

    def _write_atomically(self, text: str) -> None:
        """Replace the unit file whole, so systemd never reads half of it."""
        temp = self._unit_path.with_name(f"{self._unit_path.name}.{os.getpid()}.tmp")
        temp.write_text(text)
        temp.replace(self._unit_path)

    @staticmethod
    def _quote(arg: str) -> str:
        """Quote *arg* for ExecStart, which has its own parser and is not a shell.

        Besides quotes and backslashes, systemd expands ``%`` specifiers inside
        ExecStart, so each is doubled to stay literal.  ``$`` needs no escape: the
        command line opens with ``:``, which turns variable substitution off.
        """
        escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
        return f'"{escaped}"'
