"""Run hook commands and record the detached worker's outcome."""

from __future__ import annotations

import logging
import os

from biff._hook_foreground import ForegroundHook

logger = logging.getLogger(__name__)


class HookDispatch:
    """Dispatch hooks after entry-point logging is configured."""

    @classmethod
    def run(cls, *, detached: bool) -> None:
        """Run a hook, recording the outcome when the worker is detached."""
        if detached:
            cls._run_detached()
        else:
            ForegroundHook.run()

    @classmethod
    def _run_detached(cls) -> None:
        pid = os.getpid()
        logger.info("Starting session cleanup: pid=%d", pid)
        try:
            ForegroundHook.invoke()
        except Exception as exc:
            logger.error(
                "Session cleanup failed: pid=%d error=%s at %s",
                pid,
                type(exc).__name__,
                ForegroundHook.failure_location(exc),
            )
            raise
        logger.info("Session cleanup finished: pid=%d", pid)
