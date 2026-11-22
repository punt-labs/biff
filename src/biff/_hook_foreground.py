"""Retain privacy-safe diagnostics for foreground hook failures."""

from __future__ import annotations

import logging

from biff._hook_entry import main as _dispatch

logger = logging.getLogger(__name__)


class ForegroundHook:
    """Invoke hooks and retain privacy-safe foreground failure diagnostics."""

    @staticmethod
    def invoke() -> None:
        """Run the hook entry point; callers own outcome logging."""
        _dispatch()

    @classmethod
    def run(cls) -> None:
        """Dispatch a foreground hook, logging unexpected failures before re-raising."""
        try:
            cls.invoke()
        except Exception as exc:
            logger.error(
                "hook_foreground_failed error=%s at %s",
                type(exc).__name__,
                cls.failure_location(exc),
            )
            raise

    @staticmethod
    def failure_location(exc: Exception) -> str:
        """Identify the innermost Biff frame without paths or exception data."""
        location = "unknown"
        frame = exc.__traceback__
        while frame is not None:
            module = frame.tb_frame.f_globals.get("__name__")
            if isinstance(module, str) and module.startswith("biff."):
                location = f"{module}.{frame.tb_frame.f_code.co_name}:{frame.tb_lineno}"
            frame = frame.tb_next
        return location
