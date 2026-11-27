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
    """The run time, aliveness and report of one service start.

    A service its supervisor shows running that does not answer is slow, not
    crashed; one the supervisor does not show running exited, and its log says
    why.  Read it before a rollback removes the service, or there is nothing to
    ask.
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

    def describe(self, ending: str) -> str:
        """Say how long the service has run, *ending* the sentence, and what shows.

        A supervisor that cannot be asked is part of the answer, not an error.
        """
        ran = f"It ran {time.monotonic() - self._started:.1f} seconds {ending}"
        try:
            alive = self._backend.is_active()
        except (ServiceActionFailedError, OSError, subprocess.TimeoutExpired) as exc:
            return f"{ran}, and the supervisor could not be asked about it ({exc})."
        verdict = (
            "showed it running, so it had not crashed: it is slow to start"
            if alive
            else "did not show it running: it exited or never started"
        )
        return f"{ran}, and the supervisor {verdict}.{self._report()}"

    def _report(self) -> str:
        """Return the supervisor's own report on the service, as one sentence."""
        if not isinstance(self._backend, Diagnosable):
            return ""
        try:
            lines = self._backend.diagnosis().splitlines()
        except (ServiceActionFailedError, OSError, subprocess.TimeoutExpired) as exc:
            return f" Its report was unavailable ({exc})."
        return f" Its report: {'; '.join(line.strip() for line in lines)}."
