"""Open a Claude Code session through biffd: hello, then the files a hook owns."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING, Final, Self, final

from biff._stdlib import biff_data_dir, find_git_root, get_repo_slug, sanitize_repo_name
from biff.client.connection import ClientIdentity
from biff.client.host import HostConnector
from biff.models import UnreadSummary
from biff.plugins.claude_code.unread_file import write_unread_file
from biff.session_handle import SessionHandleStore
from biff.session_registry import write_marker
from biff.session_routing import SessionIdError, SessionRouting

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from biff.client.connection import DaemonClient

__all__ = ["DEADLINE_SECONDS", "DaemonSessionOpener"]

DEADLINE_SECONDS: Final = 8.0
"""How long SessionStart waits for biffd: it may have to start it first."""


@final
class DaemonSessionOpener:
    """Registers a session with biffd and leaves the files the hook owns.

    The hello creates the session's presence row.  A ``status`` request then
    names the key and tty alias biffd chose, and the opener writes the active
    marker, the unread status file and the handle from them.  The connection is
    closed at once: the session outlives it, held for its retention, and the
    MCP server attaches its own connection on its first call.
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
        """Open sessions with *connect*, giving biffd *deadline* seconds."""
        self = super().__new__(cls)
        self._connect = connect
        self._unread_dir = unread_dir
        self._handles = handles
        self._deadline = deadline
        return self

    @classmethod
    def for_host(cls) -> Self:
        """Return the opener for this host's biffd, started on demand."""
        data_root = biff_data_dir()
        return cls(
            connect=HostConnector.for_host().connect,
            unread_dir=data_root / "unread",
            handles=SessionHandleStore(data_root / "sessions"),
            deadline=DEADLINE_SECONDS,
        )

    async def open(self, data: dict[str, object]) -> str | None:
        """Open the session a SessionStart payload names; return its session key.

        Returns ``None`` when the payload carries no usable ``session_id``: the
        harness failed to declare itself, and a hook never breaks session start
        over that.

        Raises:
            TimeoutError: biffd did not answer within the deadline.
            ConnectionError: biffd is absent, cannot start, or sent an
                unreadable reply.
            DaemonError: biffd refused the hello.
        """
        raw = data.get("session_id")
        try:
            routing = SessionRouting(raw if isinstance(raw, str) else "")
        except SessionIdError:
            return None
        cwd = data.get("cwd")
        where = cwd if isinstance(cwd, str) and cwd else os.getcwd()
        async with asyncio.timeout(self._deadline):
            status = await self._status(routing, where)
        key = str(status["session_key"])
        root = find_git_root(Path(where)) or Path(where)
        repo_name = sanitize_repo_name(get_repo_slug(root) or root.name)
        write_marker(repo_name, key, str(root))
        write_unread_file(
            routing.unread_path(self._unread_dir),
            UnreadSummary(count=0),
            repo_name=repo_name,
            user=str(status["user"]),
            tty_name=str(status["tty_name"]),
            biff_enabled=True,
        )
        self._handles.write(routing, key)
        return key

    async def _status(self, routing: SessionRouting, cwd: str) -> dict[str, object]:
        client = await self._connect(
            ClientIdentity(client="hook", session_id=routing.session_id, cwd=cwd)
        )
        try:
            reply = await client.request("status")
        finally:
            await client.close()
        if not isinstance(reply, dict) or not isinstance(reply.get("data"), dict):
            msg = "biffd answered status without the session's facts"
            raise ConnectionError(msg)
        return dict(reply["data"])
