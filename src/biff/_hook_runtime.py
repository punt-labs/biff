"""Configure diagnostics before dispatching the lightweight hook executable."""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass

from punt_observe.logging import LoggingSetupError, Role

from biff._hook_entry import main as _dispatch
from biff.logging_config import configure_logging

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HookInvocation:
    """Configure and dispatch one hook invocation."""

    detached: bool

    @classmethod
    def main(cls) -> None:
        """Run the hook selected by the process arguments."""
        cls(detached=sys.argv[1:3] == ["claude-code", "session-end"]).run()

    def run(self) -> None:
        """Run the hook, retaining detached cleanup outcomes in the durable sink."""
        try:
            configure_logging(Role.WORKER if self.detached else Role.HOOK)
        except LoggingSetupError as exc:
            # A diagnostic outage must not bypass the edit gate or caller stamping.
            sys.stderr.write(f"biff-hook: {exc}\n")
        if self.detached:
            self._run_detached()
        else:
            _dispatch()

    def _run_detached(self) -> None:
        pid = os.getpid()
        logger.info("Starting session cleanup: pid=%d", pid)
        try:
            _dispatch()
        except Exception as exc:
            logger.error(
                "Session cleanup failed: pid=%d error=%s", pid, type(exc).__name__
            )
            raise
        logger.info("Session cleanup finished: pid=%d", pid)


main = HookInvocation.main
