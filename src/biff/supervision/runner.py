"""The one place that starts a process on behalf of the service manager."""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING, Final, final

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["Supervisor"]

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS: Final = 30.0


@final
class Supervisor:
    """Runs ``systemctl``, ``launchctl``, ``claude`` and a detached ``biffd``."""

    @staticmethod
    def run(*argv: str) -> subprocess.CompletedProcess[str]:
        """Run *argv* and return its result; a non-zero exit is the caller's to read.

        Raises:
            FileNotFoundError: The program is not installed.
            subprocess.TimeoutExpired: The supervisor did not answer in 30 seconds.
        """
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=_TIMEOUT_SECONDS,
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
    def run_visible(*argv: str) -> int:
        """Run *argv* on the user's terminal with no time limit; return its exit code.

        For a program that talks to the user, such as ``claude plugin install``.

        Raises:
            FileNotFoundError: The program is not installed.
        """
        return subprocess.run(argv, check=False).returncode

    @staticmethod
    def spawn_detached(argv: Sequence[str]) -> None:
        """Start *argv* in its own session, detached from this process's terminal.

        The child outlives this process.  Its streams go nowhere: a daemon writes
        its diagnostics to its own log.
        """
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
