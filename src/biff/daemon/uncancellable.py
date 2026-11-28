"""Work that a cancelled caller cannot stop, and that is reported if it fails."""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
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

        A failure that reaches nobody, because the caller left first, is logged.
        """
        task = asyncio.create_task(work, name=name)
        Uncancellable._running.add(task)
        task.add_done_callback(Uncancellable._running.discard)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():  # the cancel and the outcome arrived together
                Uncancellable._report(name, task)
            else:
                task.add_done_callback(partial(Uncancellable._report, name))
            raise

    @staticmethod
    async def holding[T](
        session: Session, read: Callable[[Session], Awaitable[T]], name: str
    ) -> T:
        """Run *read* while holding *session*; the caller's cancel does not stop it.

        Waiting for the session is the caller's own and can be cancelled: a read
        that gave up before it took the session never starts, so it cannot fetch
        ahead of the reads behind it.  Once *read* holds the session it is
        shielded, because a fetch that consumed mail must run on to where the
        mail is held, or a request timeout would drop it.  The session stays
        held until then, so the next read waits and finds the mail.
        """
        held = AsyncExitStack()
        await held.enter_async_context(session.exclusive())

        async def under_lock() -> T:
            async with held:
                return await read(session)

        return await Uncancellable.run(under_lock(), name)

    @staticmethod
    async def settle(timeout: float) -> None:
        """Wait up to *timeout* seconds for the steps running to finish."""
        if running := tuple(Uncancellable._running):
            _, left = await asyncio.wait(running, timeout=timeout)
            if left:
                logger.warning("%d uncancellable steps outlived the wait", len(left))

    @staticmethod
    def _report(name: str, done: asyncio.Task[object]) -> None:
        if not done.cancelled() and (exc := done.exception()) is not None:
            logger.error("%s failed after its caller left", name, exc_info=exc)
