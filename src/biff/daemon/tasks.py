"""Background tasks that never die silently."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, ClassVar, final

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

__all__ = ["Supervised"]

logger = logging.getLogger(__name__)


@final
class Supervised:
    """Starts tasks, keeps them referenced, and reports one that raised."""

    _live: ClassVar[set[asyncio.Task[None]]] = set()

    @staticmethod
    def spawn(
        work: Coroutine[object, object, None],
        name: str,
        on_death: Callable[[BaseException], None] | None = None,
    ) -> asyncio.Task[None]:
        """Run *work* as a task called *name*.

        A task that ends with an exception is logged at ERROR with its traceback
        and handed to *on_death*; cancellation is a normal end.
        """
        task = asyncio.create_task(work, name=name)
        Supervised._live.add(task)

        def finished(done: asyncio.Task[None]) -> None:
            Supervised._live.discard(done)
            if done.cancelled():
                return
            exc = done.exception()
            if exc is None:
                return
            logger.error("%s task died", name, exc_info=exc)
            if on_death is not None:
                on_death(exc)

        task.add_done_callback(finished)
        return task
