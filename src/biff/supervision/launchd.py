"""The macOS backend: a LaunchAgent in ``~/Library/LaunchAgents``."""

from __future__ import annotations

import os
import plistlib
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from biff.supervision.errors import ServiceActionFailedError
from biff.supervision.runner import Supervisor

if TYPE_CHECKING:
    from biff.supervision.spec import ServiceSpec

__all__ = ["LaunchdBackend"]


@final
class LaunchdBackend:
    """Runs one service as a per-user LaunchAgent; needs no sudo."""

    __slots__ = ("_plist_path", "_spec", "_target")

    _spec: ServiceSpec
    _plist_path: Path
    _target: str

    def __new__(cls, spec: ServiceSpec) -> Self:
        """Create the backend for *spec*."""
        self = super().__new__(cls)
        self._spec = spec
        self._plist_path = (
            Path.home() / "Library" / "LaunchAgents" / f"{spec.launchd_label}.plist"
        )
        self._target = f"{cls._domain()}/{spec.launchd_label}"
        return self

    @staticmethod
    def available() -> bool:
        """Return whether ``launchctl`` is on this host."""
        try:
            return Supervisor.run("launchctl", "version").returncode == 0
        except FileNotFoundError:
            return False

    def config_path(self) -> Path:
        """Return the plist path."""
        return self._plist_path

    def plist_bytes(self) -> bytes:
        """Return the plist for the service."""
        keep_alive: bool | dict[str, bool] = (
            {"SuccessfulExit": False} if self._spec.restart_on_crash_only else True
        )
        local_bin = str(Path.home() / ".local" / "bin")
        return plistlib.dumps(
            {
                "Label": self._spec.launchd_label,
                "ProgramArguments": self._spec.resolve_exec_args(),
                "RunAtLoad": True,
                "KeepAlive": keep_alive,
                "EnvironmentVariables": {
                    "PATH": f"{local_bin}:/usr/local/bin:/usr/bin:/bin",
                },
            }
        )

    def install(self) -> bool:
        """Write the plist and load it; the load starts the service.

        Returns:
            True when the installed plist already matched and the service runs.
        """
        content = self.plist_bytes()
        unchanged = self._read_installed() == content
        if unchanged and self.is_active():
            return True
        self._plist_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = self._plist_path.with_name(f"{self._plist_path.name}.{os.getpid()}.tmp")
        temp.write_bytes(content)
        temp.replace(self._plist_path)
        self._unload()
        self._require("bootstrap", self._domain(), str(self._plist_path))
        return False

    def uninstall(self) -> None:
        """Unload and remove the plist; a missing plist is not an error."""
        self._unload()
        self._plist_path.unlink(missing_ok=True)

    def enable_autostart(self) -> bool:
        """Enable the job so it loads at login."""
        return Supervisor.succeeds("launchctl", "enable", self._target)

    def start(self) -> bool:
        """Load the plist when it is not loaded, then start the job."""
        if not self._loaded() and not Supervisor.succeeds(
            "launchctl", "bootstrap", self._domain(), str(self._plist_path)
        ):
            return False
        return Supervisor.succeeds("launchctl", "kickstart", self._target)

    def stop(self) -> bool:
        """Unload the job so KeepAlive does not bring it back; the plist stays."""
        return not self._loaded() or Supervisor.succeeds(
            "launchctl", "bootout", self._target
        )

    def restart(self) -> bool:
        """Kill and restart the loaded job in one call; load it first if needed."""
        if not self._loaded():
            return self.start()
        return Supervisor.succeeds("launchctl", "kickstart", "-k", self._target)

    def is_active(self) -> bool:
        """Return whether launchd reports the job running."""
        result = Supervisor.run("launchctl", "print", self._target)
        return result.returncode == 0 and "state = running" in result.stdout

    def linger_warning(self) -> str:
        """Return no warning: a LaunchAgent survives logout of the shell."""
        return ""

    @staticmethod
    def _domain() -> str:
        return f"gui/{os.getuid()}"

    def _loaded(self) -> bool:
        return Supervisor.run("launchctl", "print", self._target).returncode == 0

    def _unload(self) -> None:
        if self._loaded():
            Supervisor.run("launchctl", "bootout", self._target)

    def _read_installed(self) -> bytes:
        try:
            return self._plist_path.read_bytes()
        except FileNotFoundError:
            return b""

    def _require(self, *args: str) -> None:
        result = Supervisor.run("launchctl", *args)
        if result.returncode != 0:
            msg = f"launchctl {' '.join(args)} failed: {result.stderr.strip()}"
            raise ServiceActionFailedError(msg)
