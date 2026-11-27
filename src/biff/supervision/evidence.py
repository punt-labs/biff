"""What a supervisor shows about a service that was started and did not answer."""

from __future__ import annotations

import subprocess
import time
from typing import TYPE_CHECKING, Final, Self, final

from biff.supervision.diagnosable import Diagnosable, PidReporting
from biff.supervision.errors import ServiceActionFailedError

if TYPE_CHECKING:
    from pathlib import Path

    from biff.supervision.backend import ServiceBackend

__all__ = ["SupervisorEvidence"]

_LOG_TAIL_BYTES: Final = 1_048_576


@final
class SupervisorEvidence:
    """How long a start was waited on, and what its supervisor shows now.

    The wait is not the process's run time: biffd may have exited early and been
    restarted.  So the supervisor's present state is reported as present, and its
    own report supplies the exit status.  Read it before a rollback removes the
    service, or there is nothing to ask.
    """

    __slots__ = ("_backend", "_log", "_started")

    _backend: ServiceBackend
    _log: Path
    _started: float

    def __new__(cls, backend: ServiceBackend, log: Path) -> Self:
        """Start the clock for a service *backend* just started; *log* is its log."""
        self = super().__new__(cls)
        self._backend = backend
        self._log = log
        self._started = time.monotonic()
        return self

    def is_starting(self) -> bool:
        """Return whether the service is running and its own startup line says so.

        The supervisor shows a process, and biffd's log holds the ``biffd starting``
        line of that same pid: it is alive and has not yet listened, which is a slow
        start and not a crash.  Anything less, or anything unreadable, is not.
        """
        try:
            if not self._backend.is_active() or not isinstance(
                self._backend, PidReporting
            ):
                return False
            pid = self._backend.main_pid()
            return pid is not None and self._startup_logged(pid)
        except (ServiceActionFailedError, OSError, subprocess.TimeoutExpired):
            return False

    def _startup_logged(self, pid: int) -> bool:
        """Return whether the log's tail holds the startup line of process *pid*."""
        with self._log.open("rb") as log:
            log.seek(0, 2)
            log.seek(max(0, log.tell() - _LOG_TAIL_BYTES))
            tail = log.read().decode(errors="replace")
        return any(
            "biffd starting:" in line and f"pid={pid} " in line
            for line in tail.splitlines()
        )

    def describe(self) -> str:
        """Say how long the wait was, what the supervisor shows now, and its report.

        A supervisor that cannot be asked is part of the answer, not an error.
        """
        waited = f"Waited {time.monotonic() - self._started:.1f} seconds."
        try:
            alive = self._backend.is_active()
        except (ServiceActionFailedError, OSError, subprocess.TimeoutExpired) as exc:
            return f"{waited} The supervisor could not be asked about it ({exc})."
        verdict = (
            "shows it running now: it is slow to start, or it exited and was "
            "restarted (its report has the last exit status)"
            if alive
            else "does not show it running: it exited or never started"
        )
        return f"{waited} The supervisor {verdict}.{self._report()}"

    def _report(self) -> str:
        """Return the supervisor's own report on the service, as one sentence."""
        if not isinstance(self._backend, Diagnosable):
            return ""
        try:
            lines = self._backend.diagnosis().splitlines()
        except (ServiceActionFailedError, OSError, subprocess.TimeoutExpired) as exc:
            return f" Its report was unavailable ({exc})."
        return f" Its report: {'; '.join(line.strip() for line in lines)}."
