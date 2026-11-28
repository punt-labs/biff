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

    The whole restart command has 30 seconds (``Supervisor.run``), so each step
    has a budget that leaves room for the next: a few seconds for reads in
    flight, then the held mail as long as it takes within a second budget.  What
    a read that is still running has not yet taken stays in the relay; what could
    not be filed in time, or is fetched after the hand-back began, is spilled to
    a file that the next start files back before it serves.
    """

    SETTLE_SECONDS: ClassVar[float] = 5.0
    REFILE_SECONDS: ClassVar[float] = 15.0
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
        """File every session's held mail back, and spill what is left."""
        core.sessions.seal(spill.hold)  # after the wait; sessions made later too
        sessions = core.sessions.snapshot()
        try:
            async with asyncio.timeout(Handback.REFILE_SECONDS):
                for key, session in sessions.items():
                    try:
                        await Handback._refile_one(session)
                    except RELAY_ERRORS:
                        logger.warning(
                            "could not give back the mail held for session %s",
                            key.session_id,
                            exc_info=True,
                        )
        except TimeoutError:
            logger.error(
                "%d held messages were not given back within %g s",
                sum(len(s.outbox) for s in sessions.values()),
                Handback.REFILE_SECONDS,
            )
        finally:
            Handback._spill_rest(sessions.values(), spill)

    @staticmethod
    async def _refile_one(session: Session) -> None:
        """Give back *session*'s held mail while no read of it is mid-fetch.

        A read fetching under the session's lock could take the copy this files
        back before the original is let go, and lose both.  So the lock is taken
        first; if a read keeps it past :attr:`LOCK_SECONDS` the mail it holds is
        spilled instead (by the caller), and the read's own late fetch with it.
        """
        async with AsyncExitStack() as held:
            try:
                async with asyncio.timeout(Handback.LOCK_SECONDS):
                    await held.enter_async_context(session.exclusive())
            except TimeoutError:
                logger.warning(
                    "a read of session %s is still fetching; its held mail is spilled",
                    session.session_id,
                )
                return
            await HeldMail.refile(session)

    @staticmethod
    def _spill_rest(sessions: Iterable[Session], spill: Spill) -> None:
        """Spill what each session still holds; one failing does not stop the rest."""
        for session in sessions:
            try:
                spill.hold(session, session.outbox.held())
            except Exception:
                logger.exception(
                    "could not spill the mail held for session %s", session.session_id
                )
