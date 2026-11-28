"""What a stopping daemon gives back to the relay before it lets go of it."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, ClassVar, final

from biff.daemon.held_mail import HeldMail
from biff.daemon.uncancellable import Uncancellable
from biff.relay_errors import RELAY_ERRORS

if TYPE_CHECKING:
    from biff.daemon.connection import DaemonCore

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
    a read that is still running has not yet taken stays in the relay.
    """

    SETTLE_SECONDS: ClassVar[float] = 5.0
    REFILE_SECONDS: ClassVar[float] = 15.0

    @staticmethod
    async def close_relays(core: DaemonCore) -> None:
        """Let started reads finish, give back held mail, and close the relays.

        Reads get :attr:`SETTLE_SECONDS` to finish; those still running after
        that are left, and the mail held so far is given back, within
        :attr:`REFILE_SECONDS` in all.  The relays are closed even if giving the
        mail back raised.
        """
        try:
            await Uncancellable.settle(Handback.SETTLE_SECONDS)
            await Handback._give_back(core)
        finally:
            await core.pool.close()

    @staticmethod
    async def _give_back(core: DaemonCore) -> None:
        """File every session's held mail back, until the budget is spent."""
        try:
            async with asyncio.timeout(Handback.REFILE_SECONDS):
                for key, session in core.sessions.snapshot().items():
                    try:
                        await HeldMail.refile(session)
                    except RELAY_ERRORS:
                        logger.warning(
                            "could not give back the mail held for session %s",
                            key.session_id,
                            exc_info=True,
                        )
        except TimeoutError:
            left = sum(len(s.outbox) for s in core.sessions.snapshot().values())
            logger.error(
                "%d held messages were not given back within %g s",
                left,
                Handback.REFILE_SECONDS,
            )
