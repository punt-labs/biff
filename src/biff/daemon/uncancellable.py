"""Work that a cancelled caller cannot stop, and that is reported if it fails."""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING, ClassVar, final

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from biff.daemon.sessions import Session

__all__ = ["Uncancellable"]

logger = logging.getLogger(__name__)


@final
class Uncancellable:
    """Runs a step that cannot be undone once started, such as a consuming fetch.

    The step must run on to where its result is held, whatever happens to the
    caller that asked for it.
    """

    _running: ClassVar[set[asyncio.Task[object]]] = set()

    @staticmethod
    async def run[T](work: Coroutine[object, object, T], name: str) -> T:
        """Return *work*'s result; cancelling the caller does not stop *work*.

        A failure after the caller left is logged, since nobody is left to
        receive it.
        """
        task = asyncio.create_task(work, name=name)
        Uncancellable._running.add(task)
        task.add_done_callback(Uncancellable._running.discard)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():  # the cancel and the result arrived together
                raise
            task.add_done_callback(partial(Uncancellable._report_orphan, name))
            raise

    @staticmethod
    async def holding[T](
        session: Session, read: Callable[[Session], Awaitable[T]], name: str
    ) -> T:
        """Run *read* while holding *session*; the caller's cancel does not stop it.

        A fetch that consumed mail must run on to where the mail is held, or a
        request timeout would drop it.  The session stays held until then, so
        the next read waits for it and finds the mail.
        """

        async def under_lock() -> T:
            async with session.exclusive():
                return await read(session)

        return await Uncancellable.run(under_lock(), name)

    @staticmethod
    def _report_orphan(name: str, done: asyncio.Task[object]) -> None:
        if not done.cancelled() and (exc := done.exception()) is not None:
            logger.error("%s failed after its caller left", name, exc_info=exc)
