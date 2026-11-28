"""Open a Claude Code session through biffd: hello, then the files a hook owns."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from biff._stdlib import (
    find_git_root,
    get_repo_common_root,
    get_repo_slug,
    sanitize_repo_name,
)
from biff.models import UnreadSummary
from biff.plugins.claude_code.hook_daemon import HookDaemon
from biff.plugins.claude_code.unread_file import write_unread_file
from biff.session_registry import write_marker

if TYPE_CHECKING:
    from biff.session_routing import SessionRouting

__all__ = ["DaemonSessionOpener"]


@final
class DaemonSessionOpener:
    """Registers a session with biffd and leaves the files the hook owns.

    The hello creates the session's presence row.  A ``status`` request then
    names the key and tty alias biffd chose, and the opener writes the active
    marker, the unread status file and the handle from them.  The connection is
    closed at once: the session outlives it, held for its retention, and the
    MCP server attaches its own connection on its first call.
    """

    __slots__ = ("_daemon",)

    _daemon: HookDaemon

    def __new__(cls, daemon: HookDaemon) -> Self:
        """Open sessions through *daemon*."""
        self = super().__new__(cls)
        self._daemon = daemon
        return self

    @classmethod
    def for_host(cls) -> Self:
        """Return the one for this host's biffd, started on demand."""
        return cls(HookDaemon.for_host())

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
        daemon = self._daemon
        if (routing := daemon.routing_of(data)) is None:
            return None
        where = daemon.cwd_of(data)
        async with daemon.deadline():
            status = await self._status(routing, where)
        key = str(status["session_key"])
        root = find_git_root(Path(where)) or Path(where)
        repo_name = sanitize_repo_name(get_repo_slug(root) or root.name)
        write_marker(repo_name, key, get_repo_common_root(where))
        write_unread_file(
            routing.unread_path(daemon.unread_dir),
            UnreadSummary(count=0),
            repo_name=repo_name,
            user=str(status["user"]),
            tty_name=str(status["tty_name"]),
            biff_enabled=True,
        )
        daemon.handles.write(routing, key)
        return key

    async def _status(self, routing: SessionRouting, cwd: str) -> dict[str, object]:
        client = await self._daemon.client(routing, cwd)
        try:
            reply = await client.request("status")
        finally:
            await client.close()
        facts = reply.get("data") if isinstance(reply, dict) else None
        if not isinstance(facts, dict):
            msg = "biffd answered status without the session's facts"
            raise ConnectionError(msg)
        return {**facts}
