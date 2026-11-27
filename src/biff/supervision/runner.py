"""The one place that runs a supervisor command."""

from __future__ import annotations

import logging
import subprocess
from typing import Final, final

__all__ = ["Supervisor"]

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS: Final = 30.0


@final
class Supervisor:
    """Runs ``systemctl`` or ``launchctl`` and reports what they answered."""

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
