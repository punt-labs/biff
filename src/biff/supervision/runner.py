"""The one place that starts a process on behalf of the service manager."""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING, ClassVar, Final, final

from biff.supervision.errors import ServiceActionFailedError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from biff.supervision.spec import ServiceSpec

__all__ = ["Supervisor"]

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS: Final = 30.0
_EXCERPT_LINES: Final = 8


@final
class Supervisor:
    """Runs ``systemctl``, ``launchctl``, ``claude`` and a detached ``biffd``."""

    STOP_TIMEOUT_SECONDS: ClassVar[float] = 60.0
    """How long a call that waits for biffd to stop may run: its stop takes up to
    33 s (``Handback.STOP_SECONDS``) and the call must outwait it, or an install
    rolls the unit back in the middle of the hand-back."""

    @staticmethod
    def run(
        *argv: str, timeout: float = _TIMEOUT_SECONDS
    ) -> subprocess.CompletedProcess[str]:
        """Run *argv* and return its result; a non-zero exit is the caller's to read.

        Raises:
            OSError: The program is missing or cannot be run.
            subprocess.TimeoutExpired: The program did not answer within *timeout*
                seconds (30 by default).
        """
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )

    @staticmethod
    def succeeds(*argv: str) -> bool:
        """Run *argv*; return whether it exited 0, logging stderr when it did not."""
        result = Supervisor.run(*argv)
        if result.returncode != 0:
            logger.warning(
                "%s failed (rc=%d): %s",
                " ".join(argv),
                result.returncode,
                result.stderr.strip(),
            )
        return result.returncode == 0

    @staticmethod
    def top_level_fields(text: str) -> dict[str, str]:
        """Return the ``key = value`` pairs one tab deep in *text*, first of each key.

        ``launchctl print`` indents a job's own keys one tab and its nested blocks
        (``endpoints``, ``coalition``) deeper, and the nested blocks repeat keys
        such as ``state`` that mean something else.
        """
        found: dict[str, str] = {}
        for line in text.splitlines():
            if line.startswith("\t") and not line.startswith("\t\t"):
                key, separator, value = line.strip().partition(" = ")
                if separator:
                    found.setdefault(key, value)
        return found

    @staticmethod
    def excerpt(*argv: str, keys: tuple[str, ...] = ()) -> str:
        """Run *argv* and return what it printed, cut to the part worth reading.

        With *keys*, the top-level ``key = value`` lines for those keys, in that
        order, as a report whose answer comes late needs; with none of them
        present, or no *keys*, the first eight lines.  A supervisor reports "not
        running" with a non-zero exit, so the exit status is not read; stderr
        stands in when stdout is empty.

        Raises:
            OSError: The program is missing or cannot be run.
            subprocess.TimeoutExpired: The supervisor did not answer in 30 seconds.
        """
        result = Supervisor.run(*argv)
        text = result.stdout.strip() or result.stderr.strip() or "no output"
        fields = Supervisor.top_level_fields(text)
        wanted = [f"{key} = {fields[key]}" for key in keys if key in fields]
        return "\n".join(wanted or text.splitlines()[:_EXCERPT_LINES])

    @staticmethod
    def require(
        spec: ServiceSpec,
        action: str,
        *argv: str,
        timeout: float | None = None,  # None: the default limit
    ) -> None:
        """Run *argv* as *spec*'s *action*; raise with the supervisor's own words.

        Raises:
            ServiceActionFailedError: *argv* exited non-zero; the error carries its
                stderr and names the log.
            OSError: The program is missing or cannot be run.
            subprocess.TimeoutExpired: The supervisor did not answer within
                *timeout* seconds (30 by default).
        """
        limit = _TIMEOUT_SECONDS if timeout is None else timeout
        result = Supervisor.run(*argv, timeout=limit)
        if result.returncode != 0:
            said = result.stderr.strip() or result.stdout.strip() or "no output"
            detail = f"`{' '.join(argv)}` exited {result.returncode}: {said}"
            raise ServiceActionFailedError(spec.failure_message(action, detail))

    @staticmethod
    def run_visible(*argv: str) -> int:
        """Run *argv* on the user's terminal with no time limit; return its exit code.

        For a program that talks to the user, such as ``claude plugin install``.

        Raises:
            OSError: The program is missing or cannot be run.
        """
        return subprocess.run(argv, check=False).returncode

    @staticmethod
    def spawn_detached(argv: Sequence[str]) -> subprocess.Popen[bytes]:
        """Start *argv* in its own session, detached from this process's terminal.

        The child outlives this process.  Its streams go nowhere: a daemon writes
        its diagnostics to its own log.

        Raises:
            OSError: The program cannot be started.
        """
        return subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
