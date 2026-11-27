"""What a backend can say about its service beyond ``ServiceBackend``."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["Diagnosable", "PidReporting"]


@runtime_checkable
class Diagnosable(Protocol):
    """A backend that reports its supervisor's view of the service.

    Apart from ``ServiceBackend`` so a punt-service backend, which has no such
    method, still satisfies that protocol as it is.
    """

    def diagnosis(self) -> str:
        """Return the first lines of the supervisor's own report on the service.

        Raises:
            OSError: The supervisor program is missing or cannot be run.
            subprocess.TimeoutExpired: The supervisor did not answer.
        """
        ...


@runtime_checkable
class PidReporting(Protocol):
    """A backend that can say which process its supervisor runs the service as."""

    def main_pid(self) -> int | None:
        """Return the service's process id, or ``None`` when none is running.

        Raises:
            OSError: The supervisor program is missing or cannot be run.
            subprocess.TimeoutExpired: The supervisor did not answer.
        """
        ...
