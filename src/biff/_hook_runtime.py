"""Configure diagnostics before dispatching the lightweight hook executable."""

from __future__ import annotations

import logging
import os
import sys

from punt_observe.logging import LoggingSetupError, Role

from biff._hook_entry import main as _dispatch
from biff.logging_config import configure_logging

logger = logging.getLogger(__name__)


def main() -> None:
    """Run the hook, retaining detached cleanup outcomes in the durable sink."""
    detached = sys.argv[1:3] == ["claude-code", "session-end"]
    try:
        configure_logging(Role.WORKER if detached else Role.HOOK)
    except LoggingSetupError as exc:
        # A diagnostic outage must not bypass the edit gate or caller stamping.
        sys.stderr.write(f"biff-hook: {exc}\n")
    if not detached:
        _dispatch()
        return

    pid = os.getpid()
    logger.info("Starting session cleanup: pid=%d", pid)
    try:
        _dispatch()
    except Exception:
        logger.error("Session cleanup failed: pid=%d", pid)
        raise
    logger.info("Session cleanup finished: pid=%d", pid)
