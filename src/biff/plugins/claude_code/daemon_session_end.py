"""End a Claude Code session through biffd and clear the files its hooks left."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Self, final

from biff.client.connection import DaemonError
from biff.frame_reply import ErrorCode
from biff.plugins.claude_code.hook_daemon import HookDaemon

if TYPE_CHECKING:
    from biff.session_routing import SessionRouting

__all__ = ["DaemonSessionCloser"]

logger = logging.getLogger(__name__)


@final
class DaemonSessionCloser:
    """Asks biffd to end the session a SessionEnd payload names.

    The hello says it only ends: biffd attaches it to the session it holds, on
    the relay that session lives on, and creates or revives nothing, so a relay
    setting changed since the session opened, or presence the MCP server already
    removed, does not matter.  The hello also names the run the hook belongs to,
    and only the session's current run ends it.  biffd deletes the presence row,
    forwards the unread mail to its owner's mailbox and frees the tty alias.

    The local files (active marker, unread status files, handle) are removed only
    once biffd confirms the end.  When biffd was never reached the caller falls
    back to the sentinel cleanup; once the end request was sent the outcome is
    uncertain, and that cleanup could reap the row of a newer run, so it is left
    to biffd's retention sweep, which forwards the mail the same way.
    """

    __slots__ = ("_daemon",)

    _daemon: HookDaemon

    def __new__(cls, daemon: HookDaemon) -> Self:
        """End sessions through *daemon*."""
        self = super().__new__(cls)
        self._daemon = daemon
        return self

    @classmethod
    def for_host(cls) -> Self:
        """Return the one for this host's biffd, started on demand."""
        return cls(HookDaemon.for_host())

    async def end(self, data: dict[str, object]) -> bool:
        """End the session *data* names; return whether biffd settled it.

        ``True``: it is ended, or belongs to a newer run, or may or may not have
        ended; no other cleanup may run.  ``False``: biffd did not take the end
        (absent, too slow, an older biffd, or holding no such session), the local
        files are untouched, and the sentinel cleanup should run.  Every failure
        is logged at WARNING with its cause.
        """
        daemon = self._daemon
        if (routing := daemon.routing_of(data)) is None:
            return False
        sent = False
        try:
            async with daemon.deadline():
                client = await daemon.client(
                    routing,
                    daemon.cwd_of(data),
                    ends=True,
                    run=daemon.run_of(os.environ),
                )
                try:
                    sent = True
                    await client.request("end")
                finally:
                    await client.close()
        except DaemonError as exc:
            if exc.code is ErrorCode.SESSION_REPLACED:
                logger.warning(
                    "session %s belongs to a newer run; left as it is: %s",
                    routing.session_id,
                    exc,
                )
                return True
            if not (sent and exc.code is ErrorCode.SESSION_ENDED):  # ended already
                return self._unsettled(routing, exc, sent=sent)
        except (ConnectionError, TimeoutError, ValueError) as exc:
            return self._unsettled(routing, exc, sent=sent)
        daemon.forget(routing)
        return True

    def _unsettled(
        self, routing: SessionRouting, cause: Exception, *, sent: bool
    ) -> bool:
        """Log why the end is not confirmed; return whether to skip the fallback."""
        name = routing.session_id
        if sent:
            logger.warning(
                "session %s may or may not have ended: %s (%s); its files are kept "
                "and biffd's retention sweep finishes it",
                name,
                cause,
                type(cause).__name__,
            )
            return True
        # biffd reads no sentinels, so the status files and handle go now; the
        # marker stays for the sentinel cleanup the caller runs next.
        self._daemon.forget(routing, marker=False)
        logger.warning(
            "could not end session %s through biffd: %s (%s); removed its local "
            "status files and using the sentinel cleanup",
            name,
            cause,
            type(cause).__name__,
        )
        return False
