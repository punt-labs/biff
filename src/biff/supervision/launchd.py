"""The macOS backend: a LaunchAgent in ``~/Library/LaunchAgents``."""

from __future__ import annotations

import os
import plistlib
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self, final

from biff.supervision.errors import ServiceActionFailedError
from biff.supervision.runner import Supervisor

if TYPE_CHECKING:
    from biff.supervision.spec import ServiceSpec

__all__ = ["LaunchdBackend"]

_THROTTLE_SECONDS = 30
# The ``launchctl print`` keys that say what became of the job.
_DIAGNOSIS_KEYS = (
    "active count",
    "state",
    "runs",
    "pid",
    "immediate reason",
    "last exit code",
    "last exit status",
)


@final
class LaunchdBackend:
    """Runs one service as a per-user LaunchAgent; needs no sudo."""

    __slots__ = ("_plist_path", "_spec", "_target")

    # launchctl bootout returns before the job is gone; a bootstrap that starts
    # too early fails with "Input/output error", so each unload waits and checks.
    UNLOAD_POLL_SECONDS: ClassVar[float] = 0.2
    UNLOAD_WAIT_SECONDS: ClassVar[float] = 10.0

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

    @classmethod
    def unavailable_reason(cls) -> str:
        """Return why launchd cannot run a LaunchAgent here, or ``""`` when it can.

        A LaunchAgent loads into the user's GUI domain, which does not exist for
        a login over SSH with no one at the console.
        """
        domain = cls._domain()
        try:
            probe = Supervisor.run("launchctl", "print", domain)
        except FileNotFoundError:
            return "launchctl is not installed"
        except subprocess.TimeoutExpired:
            return f"`launchctl print {domain}` timed out"
        except OSError as exc:
            return f"cannot run launchctl: {exc}"
        if probe.returncode == 0:
            return ""
        said = probe.stderr.strip() or f"exited {probe.returncode}"
        return (
            f"`launchctl print {domain}` failed (no GUI session for this user?): {said}"
        )

    @classmethod
    def available(cls) -> bool:
        """Return whether launchd can run a LaunchAgent for this user."""
        return not cls.unavailable_reason()

    def config_path(self) -> Path:
        """Return the plist path."""
        return self._plist_path

    def plist_bytes(self) -> bytes:
        """Return the plist for the service.

        Output goes to the biff log, with ``~`` expanded because launchd does not.
        ``ThrottleInterval`` bounds a crash loop, which launchd restarts forever.
        """
        keep_alive: bool | dict[str, bool] = (
            {"SuccessfulExit": False} if self._spec.restart_on_crash_only else True
        )
        local_bin = str(Path.home() / ".local" / "bin")
        log = str(Path(self._spec.log_path).expanduser())
        return plistlib.dumps(
            {
                "Label": self._spec.launchd_label,
                "ProgramArguments": self._spec.resolve_exec_args(),
                "RunAtLoad": True,
                "KeepAlive": keep_alive,
                "ThrottleInterval": _THROTTLE_SECONDS,
                "StandardOutPath": log,
                "StandardErrorPath": log,
                "EnvironmentVariables": {
                    "PATH": f"{local_bin}:/usr/local/bin:/usr/bin:/bin",
                },
            }
        )

    def install(self) -> bool:
        """Write the plist and load it; the load starts the service.

        A failure leaves no plist behind, so a later check never finds half an
        install.

        Returns:
            True when the installed plist already matched and the service runs.

        Raises:
            ServiceActionFailedError: launchctl refused; the error carries its stderr.
        """
        content = self.plist_bytes()
        unchanged = self._read_installed() == content
        if unchanged and self.is_active():
            return True
        self._plist_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        Path(self._spec.log_path).expanduser().parent.mkdir(
            mode=0o700, parents=True, exist_ok=True
        )
        temp = self._plist_path.with_name(f"{self._plist_path.name}.{os.getpid()}.tmp")
        temp.write_bytes(content)
        temp.replace(self._plist_path)
        try:
            self._unload()
            self._require("install", "bootstrap", self._domain(), str(self._plist_path))
        except (ServiceActionFailedError, OSError, subprocess.TimeoutExpired):
            self._plist_path.unlink(missing_ok=True)
            raise
        return False

    def uninstall(self) -> None:
        """Unload and remove the plist; a missing plist is not an error.

        The plist is deleted only after launchd confirms the job is gone.

        Raises:
            ServiceActionFailedError: launchctl refused, or the job is still loaded.
        """
        self._unload()
        self._plist_path.unlink(missing_ok=True)

    def enable_autostart(self) -> bool:
        """Enable the job so it loads at login."""
        return Supervisor.succeeds("launchctl", "enable", self._target)

    def start(self) -> bool:
        """Load the plist when it is not loaded, then start the job.

        Raises:
            ServiceActionFailedError: launchctl refused; the error carries its stderr.
        """
        if not self._loaded():
            self._require("start", "bootstrap", self._domain(), str(self._plist_path))
        self._require("start", "kickstart", self._target)
        return True

    def stop(self) -> bool:
        """Unload the job so KeepAlive does not bring it back; the plist stays.

        Raises:
            ServiceActionFailedError: launchctl refused, or the job is still loaded.
        """
        self._unload()
        return True

    def restart(self) -> bool:
        """Stop the job with SIGTERM, wait until it is gone, then load and start it.

        ``kickstart -k`` would SIGKILL biffd, so its shutdown handler would not run
        and its port file would stay.  Unloading is a SIGTERM, as systemd's restart
        is, so the two backends behave alike.

        Raises:
            ServiceActionFailedError: launchctl refused; the error carries its stderr.
        """
        self._unload()
        return self.start()

    def is_active(self) -> bool:
        """Return whether launchd reports the job running."""
        result = Supervisor.run("launchctl", "print", self._target)
        return result.returncode == 0 and "state = running" in result.stdout

    def diagnosis(self) -> str:
        """Return the state, run count and last exit code from ``launchctl print``.

        ``launchctl`` prints the exit code after the job's program, arguments and
        environment, so the first lines would not hold it.
        """
        return Supervisor.excerpt(
            "launchctl", "print", self._target, keys=_DIAGNOSIS_KEYS
        )

    def linger_warning(self) -> str:
        """Return no warning: a LaunchAgent survives logout of the shell."""
        return ""

    @staticmethod
    def _domain() -> str:
        return f"gui/{os.getuid()}"

    def _loaded(self) -> bool:
        return Supervisor.run("launchctl", "print", self._target).returncode == 0

    def _unload(self) -> None:
        """Boot the job out if it is loaded, and wait until launchd has let it go."""
        if not self._loaded():
            return
        self._require("stop", "bootout", self._target)
        deadline = time.monotonic() + self.UNLOAD_WAIT_SECONDS
        while self._loaded():
            if time.monotonic() >= deadline:
                msg = self._spec.failure_message(
                    "stop", f"{self._target} is still loaded after bootout"
                )
                raise ServiceActionFailedError(msg)
            time.sleep(self.UNLOAD_POLL_SECONDS)

    def _read_installed(self) -> bytes:
        try:
            return self._plist_path.read_bytes()
        except FileNotFoundError:
            return b""

    def _require(self, action: str, *args: str) -> None:
        Supervisor.require(self._spec, action, "launchctl", *args)
