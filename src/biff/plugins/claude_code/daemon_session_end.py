"""End a Claude Code session through biffd and clear the files its hooks left."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Final, Self, final

from biff._stdlib import biff_data_dir, remove_active_session
from biff.client.connection import ClientIdentity
from biff.client.host import HostConnector
from biff.session_handle import SessionHandleStore
from biff.session_routing import SessionIdError, SessionRouting

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from biff.client.connection import DaemonClient

__all__ = ["DEADLINE_SECONDS", "DaemonSessionCloser"]

DEADLINE_SECONDS: Final = 8.0
"""Under the 10 s the SessionEnd script allows its detached call."""


@final
class DaemonSessionCloser:
    """Asks biffd to end the session a SessionEnd payload names.

    biffd deletes the presence row, forwards the session's unread mail to its
    owner's mailbox and frees the tty alias; ending a session already ended
    succeeds.  Afterwards the closer removes the active marker, the unread
    status files and the handle, which no one else owns.
    """

    __slots__ = ("_connect", "_deadline", "_handles", "_unread_dir")

    _connect: Callable[[ClientIdentity], Awaitable[DaemonClient]]
    _unread_dir: Path
    _handles: SessionHandleStore
    _deadline: float

    def __new__(
        cls,
        *,
        connect: Callable[[ClientIdentity], Awaitable[DaemonClient]],
        unread_dir: Path,
        handles: SessionHandleStore,
        deadline: float,
    ) -> Self:
        """End sessions with *connect*, giving biffd *deadline* seconds."""
        self = super().__new__(cls)
        self._connect = connect
        self._unread_dir = unread_dir
        self._handles = handles
        self._deadline = deadline
        return self

    @classmethod
    def for_host(cls) -> Self:
        """Return the closer for this host's biffd, started on demand."""
        data_root = biff_data_dir()
        return cls(
            connect=HostConnector.for_host().connect,
            unread_dir=data_root / "unread",
            handles=SessionHandleStore(data_root / "sessions"),
            deadline=DEADLINE_SECONDS,
        )

    async def end(self, data: dict[str, object]) -> None:
        """End the session *data* names; do nothing when it names none.

        Raises:
            TimeoutError: biffd did not answer within the deadline.
            ConnectionError: biffd is absent, cannot start, or sent an
                unreadable reply.
            DaemonError: biffd refused the request.
        """
        raw = data.get("session_id")
        try:
            routing = SessionRouting(raw if isinstance(raw, str) else "")
        except SessionIdError:
            return
        cwd = data.get("cwd")
        where = cwd if isinstance(cwd, str) and cwd else os.getcwd()
        async with asyncio.timeout(self._deadline):
            client = await self._connect(
                ClientIdentity(client="hook", session_id=routing.session_id, cwd=where)
            )
            try:
                await client.request("end")
            finally:
                await client.close()
        self._clear(routing)

    def _clear(self, routing: SessionRouting) -> None:
        """Remove the marker, status files and handle of an ended session."""
        if (key := self._handles.read(routing)) is not None:
            remove_active_session(key)
        unread = routing.unread_path(self._unread_dir)
        for path in (unread, *map(unread.with_suffix, (".nudged", ".active"))):
            path.unlink(missing_ok=True)
        self._handles.remove(routing)
