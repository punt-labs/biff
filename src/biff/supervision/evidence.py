"""What a supervisor shows about a service that was started and did not answer."""

from __future__ import annotations

import subprocess
import time
from typing import TYPE_CHECKING, Self, final

from biff.supervision.diagnosable import Diagnosable
from biff.supervision.errors import ServiceActionFailedError

if TYPE_CHECKING:
    from biff.supervision.backend import ServiceBackend

__all__ = ["SupervisorEvidence"]


@final
class SupervisorEvidence:
    """How long a start was waited on, and what its supervisor shows now.

    The wait is not the process's run time: biffd may have exited early and been
    restarted.  So the supervisor's present state is reported as present, and its
    own report supplies the exit status.  Read it before a rollback removes the
    service, or there is nothing to ask.
    """

    __slots__ = ("_backend", "_started")

    _backend: ServiceBackend
    _started: float

    def __new__(cls, backend: ServiceBackend) -> Self:
        """Start the clock for a service *backend* has just started."""
        self = super().__new__(cls)
        self._backend = backend
        self._started = time.monotonic()
        return self

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
