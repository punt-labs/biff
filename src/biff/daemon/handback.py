"""What a stopping daemon gives back to the relay before it lets go of it."""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, ClassVar, final

from biff.daemon.held_mail import HeldMail
from biff.daemon.uncancellable import Uncancellable
from biff.relay_errors import RELAY_ERRORS

if TYPE_CHECKING:
    from collections.abc import Iterable

    from biff.daemon.connection import DaemonCore
    from biff.daemon.sessions import Session
    from biff.daemon.spill import Spill

__all__ = ["Handback"]

logger = logging.getLogger(__name__)


@final
class Handback:
    """Returns the mail a stopping daemon holds, then closes the relays.

    A consuming relay removed that mail at fetch, and a read still fetching when
    the daemon stops finishes after its caller left: the mail would exist only
    in the memory of a process that is about to exit, so a restart would not
    find it.

    Each step has a budget that leaves room for the next: a few seconds for the
    listener to close, a few for reads in flight, then the held mail as long as
    it takes within a last budget; :attr:`STOP_SECONDS` is the sum, and the
    supervisor waits longer than that before it kills the process.  What a read
    that is still running has not yet taken stays in the relay.  Everything held
    is written to the spill file as soon as the outboxes are sealed, before any
    of it is filed, so a process killed at any point after that has the mail on
    disk; what is filed is removed from the file as it goes.  What is fetched
    after the seal is spilled too, and the next start files it back before it
    serves.
    """

    CLOSE_SECONDS: ClassVar[float] = 5.0
    """The most the stop waits for the listener to close its connections."""
    SETTLE_SECONDS: ClassVar[float] = 5.0
    REFILE_SECONDS: ClassVar[float] = 15.0
    STOP_SECONDS: ClassVar[float] = CLOSE_SECONDS + SETTLE_SECONDS + REFILE_SECONDS
    LOCK_SECONDS: ClassVar[float] = 2.0
    """How long to wait for a read of a session to let go of it before spilling."""

    @staticmethod
    async def close_relays(core: DaemonCore, spill: Spill) -> None:
        """Let started reads finish, give back held mail, and close the relays.

        Reads get :attr:`SETTLE_SECONDS` to finish; those still running after
        that are left, and the mail held so far is given back, within
        :attr:`REFILE_SECONDS` in all.  Whatever could not be given back, or
        arrives from a read that ends later, goes to *spill* for the next start.
        The relays are closed even if giving the mail back raised.
        """
        try:
            await Uncancellable.settle(Handback.SETTLE_SECONDS)
            await Handback._give_back(core, spill)
        finally:
            await core.pool.close()

    @staticmethod
    async def _give_back(core: DaemonCore, spill: Spill) -> None:
        """Spill everything held, then file it back and remove what was filed."""
        core.sessions.seal(spill.hold)  # after the wait; sessions made later too
        sessions = core.sessions.snapshot()
        Handback._spill_all(sessions.values(), spill)  # the durable copy first
        try:
            async with asyncio.timeout(Handback.REFILE_SECONDS):
                for key, session in sessions.items():
                    try:
                        await Handback._refile_one(session, spill)
                    except RELAY_ERRORS:
                        logger.warning(
                            "could not give back the mail held for session %s",
                            key.session_id,
                            exc_info=True,
                        )
        except TimeoutError:
            logger.error(
                "%d held messages were not given back within %g s; they are spilled",
                sum(len(s.outbox) for s in sessions.values()),
                Handback.REFILE_SECONDS,
            )

    @staticmethod
    async def _refile_one(session: Session, spill: Spill) -> None:
        """Give back *session*'s held mail while no read of it is mid-fetch.

        A read fetching under the session's lock could take the copy this files
        back before the original is let go, and lose both.  So the lock is taken
        first; if a read keeps it past :attr:`LOCK_SECONDS` the mail stays in the
        spill, and the read's own late fetch goes there too.  What was filed
        (the outbox no longer holds it) is removed from the spill, even when the
        budget ends the filing midway.
        """
        async with AsyncExitStack() as held:
            try:
                async with asyncio.timeout(Handback.LOCK_SECONDS):
                    await held.enter_async_context(session.exclusive())
            except TimeoutError:
                logger.warning(
                    "a read of session %s is still fetching; its held mail stays "
                    "in the spill",
                    session.session_id,
                )
                return
            before = session.outbox.held()
            try:
                await HeldMail.refile(session)
            finally:
                still = {m.id for m in session.outbox.held()}
                spill.release(session, [m for m in before if m.id not in still])

    @staticmethod
    def _spill_all(sessions: Iterable[Session], spill: Spill) -> None:
        """Spill what each session holds; one failing does not stop the rest."""
        for session in sessions:
            try:
                spill.hold(session, session.outbox.held())
            except Exception:
                logger.exception(
                    "could not spill the mail held for session %s", session.session_id
                )
