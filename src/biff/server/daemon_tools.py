"""The read-only tools, answered by biffd: who, finger, last and read_messages.

Each tool asks the daemon to run the command and returns the text of its reply
unchanged.  The text is what ``plugin/hooks/suppress-output.sh`` cuts into the
panel line and the model's context, so nothing here reformats it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from fastmcp.exceptions import ToolError

from biff.client.connection import DaemonError
from biff.relay_errors import RELAY_ERRORS
from biff.server.after_call import AfterCall
from biff.server.mcp_link import FAILURES, Reply
from biff.server.tools._activity import track_activity
from biff.server.tools._descriptions import refresh_read_messages
from biff.server.tools._session import get_or_create_session, update_current_session

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from pydantic import JsonValue

    from biff.server.mcp_link import McpLink
    from biff.server.state import ServerState

__all__ = ["DaemonReadTools"]

logger = logging.getLogger(__name__)

# What each tool says about itself.  The in-process tools say the same, and a test
# fails when the two servers list a tool differently.
WHO_DESCRIPTION = "List all active team members and what they're working on."
FINGER_DESCRIPTION = "Check what a user is working on and their availability."
LAST_DESCRIPTION = "Show session login/logout history (like Unix last)."
READ_MESSAGES_DESCRIPTION = "Check your inbox for new messages. Marks all as read."
READ_MESSAGES_META = {"anthropic/alwaysLoad": True}

_NO_SESSION = "this call named no session; run biff doctor, then restart the session"


@final
class DaemonReadTools:
    """Registers the read-only tools on a server, backed by one link to biffd."""

    __slots__ = ("_link", "_mcp", "_state")

    _state: ServerState
    _link: McpLink
    _mcp: FastMCP[ServerState]

    def __new__(
        cls, mcp: FastMCP[ServerState], state: ServerState, link: McpLink
    ) -> Self:
        """Serve the tools of *mcp* for *state*'s sessions through *link*."""
        self = super().__new__(cls)
        self._mcp = mcp
        self._state = state
        self._link = link
        return self

    def register(self) -> None:
        """Register ``who``, ``finger``, ``last`` and ``read_messages``."""
        guard = track_activity(self._state)
        self._mcp.tool(name="who", description=WHO_DESCRIPTION)(guard(self.who))
        self._mcp.tool(name="finger", description=FINGER_DESCRIPTION)(
            guard(self.finger)
        )
        self._mcp.tool(name="last", description=LAST_DESCRIPTION)(guard(self.last))
        self._mcp.tool(
            name="read_messages",
            description=READ_MESSAGES_DESCRIPTION,
            meta=READ_MESSAGES_META,
        )(guard(self.read_messages))

    async def who(self) -> str:
        """List live sessions and what they are working on."""
        await update_current_session(self._state)
        await refresh_read_messages(self._mcp, self._state)
        return (await self._ask("who")).text

    async def finger(self, user: str) -> str:
        """Inspect a teammate's sessions, availability, and current plan.

        Args:
            user: Space-separated addresses. Use a bare user for all their sessions
                or user:tty to select one session; an @ prefix is optional.

        Returns:
            Presence blocks for matching live sessions. Missing users or sessions
            are described in text; an empty input produces no blocks.
        """
        await update_current_session(self._state)
        await refresh_read_messages(self._mcp, self._state)
        return (await self._ask("finger", user=user)).text

    async def last(self, user: str = "", count: int = 25) -> str:
        """Inspect recent login and logout history.

        Args:
            user: Filter by username, optionally prefixed with @. Empty means all users.
            count: Maximum displayed sessions, clamped to the range 1 through 100.

        Returns:
            Formatted session history, marking active sessions as still logged in,
            or 'No session history.' when no events match.
        """
        await update_current_session(self._state)
        return (await self._ask("last", user=user, count=count)).text

    async def read_messages(self) -> str:
        """Return unread mail; biffd marks it read once this call has returned."""
        # An empty poll is not work: it keeps the presence row alive and no more.
        await self._presence_kept()
        try:
            held = await self._link.ask_held(self._session(), "read")
            wire = Reply.parse(held.result)
        except (*FAILURES, DaemonError) as exc:
            raise ToolError(str(exc)) from exc
        AfterCall.defer(held.confirm)
        AfterCall.defer(self._refreshed)
        if wire.more > 0:
            return f"{wire.text}\n{wire.more} more unread; call read_messages again"
        return wire.text

    async def _presence_kept(self) -> None:
        """Keep the presence row alive; a relay that cannot is not a failed read."""
        try:
            await get_or_create_session(self._state)
        except RELAY_ERRORS as exc:
            logger.warning("read_messages: presence update failed: %s", exc)

    async def _refreshed(self) -> None:
        await refresh_read_messages(self._mcp, self._state)

    async def _ask(self, method: str, **params: JsonValue) -> Reply:
        try:
            return await self._link.ask(self._session(), method, **params)
        except (*FAILURES, DaemonError) as exc:
            raise ToolError(str(exc)) from exc

    def _session(self) -> str:
        session = self._state.bound_session_id
        if session is None:
            raise ToolError(_NO_SESSION)
        return session
