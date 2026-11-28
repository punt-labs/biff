"""Build again, at daemon start, the harness sessions the last run held."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, final

from biff.daemon.resolver import ResolveError
from biff.relay_errors import RELAY_ERRORS

if TYPE_CHECKING:
    from biff.daemon.connection import DaemonCore
    from biff.daemon.harness_records import HarnessRecords

__all__ = ["SessionRestore"]

logger = logging.getLogger(__name__)


@final
class SessionRestore:
    """Gives a restarted daemon the sessions it held, so none of them is forgotten."""

    @staticmethod
    async def run(records: HarnessRecords, core: DaemonCore) -> None:
        """Restore every kept session, dormant, as far as its relay answers.

        Each is opened as a client that connected and left, which is what a
        session with no connection is: retention, an end, and the mail waiting in
        its relay inbox then reach it as for any session, and a hello reattaches
        it.  A session that cannot be restored now (its repository is gone, or
        the relay is down) keeps its record, and the next start tries it again.
        """
        for hello in records.load():
            try:
                resolved = await core.resolver.resolve(hello)
                (await core.sessions.open(hello, resolved, core.pool)).detach()
            except (ResolveError, *RELAY_ERRORS):
                logger.warning(
                    "could not restore session %s", hello.session_id, exc_info=True
                )
