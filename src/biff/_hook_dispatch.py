"""Run hook commands and record the detached worker's outcome."""

from __future__ import annotations

import logging
import os
from functools import partial

from biff._hook_foreground import dispatch, invoke as _dispatch

logger = logging.getLogger(__name__)


class HookDispatch:
    """Dispatch hooks after entry-point logging is configured."""

    @classmethod
    def run(cls, *, detached: bool) -> None:
        """Run a hook, recording the outcome when the worker is detached."""
        (cls._run_detached if detached else partial(dispatch, cls._failure_location))()

    @classmethod
    def _run_detached(cls) -> None:
        pid = os.getpid()
        logger.info("Starting session cleanup: pid=%d", pid)
        try:
            _dispatch()
        except Exception as exc:
            logger.error(
                "Session cleanup failed: pid=%d error=%s at %s",
                pid,
                type(exc).__name__,
                cls._failure_location(exc),
            )
            raise
        logger.info("Session cleanup finished: pid=%d", pid)

    @staticmethod
    def _failure_location(exc: Exception) -> str:
        """Identify the innermost Biff frame without paths or exception data."""
        location = "unknown"
        frame = exc.__traceback__
        while frame is not None:
            module = frame.tb_frame.f_globals.get("__name__")
            if isinstance(module, str) and module.startswith("biff."):
                location = f"{module}.{frame.tb_frame.f_code.co_name}:{frame.tb_lineno}"
            frame = frame.tb_next
        return location
