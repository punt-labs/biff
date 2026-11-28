"""What a stopping daemon gives back to the relay before it lets go of it."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, final

from biff.daemon.session_end import SessionEnd
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
    """

    @staticmethod
    async def close_relays(core: DaemonCore) -> None:
        """Let started reads finish, give back held mail, and close the relays.

        Reads get ``core.request_timeout`` seconds to finish.  The relays are
        closed even if giving the mail back raised.
        """
        try:
            await Uncancellable.settle(core.request_timeout)
            for key, session in core.sessions.snapshot().items():
                try:
                    await SessionEnd.refile_held(session)
                except RELAY_ERRORS:
                    logger.warning(
                        "could not give back the mail held for session %s",
                        key.session_id,
                        exc_info=True,
                    )
        finally:
            await core.pool.close()
