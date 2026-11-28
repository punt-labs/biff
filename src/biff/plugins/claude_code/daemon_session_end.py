"""End a Claude Code session through biffd and clear the files its hooks left."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Self, final

from biff._stdlib import remove_active_session
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
    forwards the unread mail to its owner's mailbox and frees the tty alias;
    ending a session already ended, or one biffd no longer holds, succeeds.
    Afterwards the closer removes the active marker, the unread status files and
    the handle.
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

    async def end(self, data: dict[str, object]) -> None:
        """End the session *data* names; do nothing when it names none.

        Raises:
            TimeoutError: biffd did not answer within the deadline.
            ConnectionError: biffd is absent, cannot start, or sent an
                unreadable reply.
            DaemonError: biffd refused the request.
        """
        daemon = self._daemon
        if (routing := daemon.routing_of(data)) is None:
            return
        run = daemon.run_of(os.environ)
        try:
            async with daemon.deadline():
                client = await daemon.client(
                    routing, daemon.cwd_of(data), ends=True, run=run
                )
                try:
                    await client.request("end")
                finally:
                    await client.close()
        except DaemonError as exc:
            if exc.code is ErrorCode.SESSION_REPLACED:
                logger.info(
                    "session %s belongs to a newer run; left", routing.session_id
                )
                return
            if exc.code is not ErrorCode.SESSION_ENDED:
                raise
        self._clear(routing)

    def _clear(self, routing: SessionRouting) -> None:
        """Remove the marker, status files and handle of an ended session."""
        handles = self._daemon.handles
        if (key := handles.read(routing)) is not None:
            remove_active_session(key)
        unread = routing.unread_path(self._daemon.unread_dir)
        for path in (unread, *map(unread.with_suffix, (".nudged", ".active"))):
            path.unlink(missing_ok=True)
        handles.remove(routing)
