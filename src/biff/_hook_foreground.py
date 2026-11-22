"""Retain privacy-safe diagnostics for foreground hook failures."""

from __future__ import annotations

import logging
from collections.abc import Callable

from biff._hook_entry import main as _dispatch

logger = logging.getLogger(__name__)


def invoke() -> None:
    """Invoke the hook entry point without changing its exception behavior."""
    _dispatch()


def dispatch(failure_location: Callable[[Exception], str]) -> None:
    """Dispatch a foreground hook, logging unexpected failures before re-raising."""
    try:
        _dispatch()
    except Exception as exc:
        logger.error(
            "hook_foreground_failed error=%s at %s",
            type(exc).__name__,
            failure_location(exc),
        )
        raise
