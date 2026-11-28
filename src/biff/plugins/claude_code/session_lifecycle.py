"""Route a SessionStart or SessionEnd payload to biffd or to the in-process path."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Self, final

from biff._stdlib import find_git_root
from biff.mcp_transport import McpTransport
from biff.plugins.claude_code.daemon_session import DaemonSessionOpener
from biff.plugins.claude_code.daemon_session_end import DaemonSessionCloser
from biff.repo_transport import RepoTransport

__all__ = ["SessionLifecycle"]


@final
class SessionLifecycle:
    """What a session hook does with its payload, by the repo's transport setting.

    With ``mcp.transport: daemon`` the session is opened and ended through biffd.
    Otherwise the in-process path runs, as before.
    """

    __slots__ = ("_data",)

    _data: dict[str, object]

    def __new__(cls, data: dict[str, object]) -> Self:
        """Handle the hook payload *data*."""
        self = super().__new__(cls)
        self._data = data
        return self

    def open(self) -> None:
        """Open the session the payload names.

        Raises:
            SystemExit: A transport setting is invalid.
            ConnectionError: biffd is absent, cannot start, or sent an
                unreadable reply.
            TimeoutError: biffd did not answer in time.
        """
        if self._transport() is McpTransport.DAEMON:
            asyncio.run(DaemonSessionOpener.for_host().open(self._data))
        else:
            from biff.session_open import open_session_from_payload  # noqa: PLC0415

            asyncio.run(open_session_from_payload(self._data))

    def end_through_biffd(self) -> bool:
        """End the session the payload names through biffd, if the transport says so.

        Returns whether the end was handled here; ``False`` leaves the in-process
        cleanup to the caller.  A ``resume`` end is handled by doing nothing: the
        session carries on under the same id.

        Raises:
            ConnectionError: biffd is absent, cannot start, or sent an
                unreadable reply.
            TimeoutError: biffd did not answer in time.
        """
        if not isinstance(self._data.get("session_id"), str):
            return False
        if self._data.get("reason") == "resume":
            # The same session id starts again at once; this end may run after
            # that start, and must not close the session it just reopened.
            return self._transport() is McpTransport.DAEMON
        if self._transport() is not McpTransport.DAEMON:
            return False
        asyncio.run(DaemonSessionCloser.for_host().end(self._data))
        return True

    def _transport(self) -> McpTransport:
        cwd = self._data.get("cwd")
        # The checkout's own root, as the MCP server reads it: a linked worktree
        # carries its own config files, which may choose another transport.
        root = find_git_root(Path(cwd) if isinstance(cwd, str) and cwd else None)
        return RepoTransport.of(root, os.environ)
