"""Shared fire-and-forget task management.

Background tasks created with ``asyncio.create_task()`` are only weakly
referenced by the event loop and can be garbage collected before completion.
This module provides a single strong-reference set and a callback factory
to prevent that.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

# Strong references prevent GC of fire-and-forget tasks (CPython weak-refs).
_background_tasks: set[asyncio.Task[None]] = set()


def fire_and_forget(
    coro: Coroutine[Any, Any, None],
    *,
    logger: logging.Logger,
    description: str,
) -> asyncio.Task[None]:
    """Schedule *coro* as a background task with GC protection and error logging.

    Returns the task for callers that need it (rare).
    """
    task: asyncio.Task[None] = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _done(t: asyncio.Task[None]) -> None:
        _background_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.warning("%s failed: %s", description, exc, exc_info=exc)

    task.add_done_callback(_done)
    return task
