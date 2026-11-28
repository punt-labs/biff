"""The read-only tools, answered by biffd: who, finger, last and read_messages.

Each tool asks the daemon to run the command and returns the text of its reply
unchanged.  The text is what ``plugin/hooks/suppress-output.sh`` cuts into the
panel line and the model's context, so nothing here reformats it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar, Self, final

from fastmcp.exceptions import ToolError

from biff.relay_errors import RELAY_ERRORS
from biff.server.daemon_calls import DaemonCalls
from biff.server.tools._activity import track_activity
from biff.server.tools._descriptions import refresh_read_messages
from biff.server.tools._session import get_or_create_session, update_current_session
from biff.tty import parse_address

if TYPE_CHECKING:
    from fastmcp import FastMCP

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


@final
class DaemonReadTools:
    """Registers the read-only tools on a server, backed by one link to biffd."""

    NAMES: ClassVar[tuple[str, ...]] = (
        "who",
        "finger",
        "last",
        "read_messages",
        "get_poll_status",
    )
    """The tools it registers: the in-process tools it replaces."""

    __slots__ = ("_calls", "_mcp", "_state")

    _state: ServerState
    _calls: DaemonCalls
    _mcp: FastMCP[ServerState]

    def __new__(
        cls, mcp: FastMCP[ServerState], state: ServerState, link: McpLink
    ) -> Self:
        """Serve the tools of *mcp* for *state*'s sessions through *link*."""
        self = super().__new__(cls)
        self._mcp = mcp
        self._state = state
        self._calls = DaemonCalls(state, link)
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
            name="get_poll_status",
            description="Show the current poll interval and whether polling is active.",
        )(self.get_poll_status)
        self._mcp.tool(
            name="read_messages",
            description=READ_MESSAGES_DESCRIPTION,
            meta=READ_MESSAGES_META,
        )(guard(self.read_messages))

    async def who(self) -> str:
        """Find teammates and session addresses in visible repositories.

        Call without arguments to inspect live sessions, idle times, and plans.

        Returns:
            A presence table, or 'No sessions.' when none are live.

        Raises:
            ToolError: biffd is not installed, did not start, or refused the call;
                the message names the cause and the command that repairs it.
        """
        await update_current_session(self._state)
        await refresh_read_messages(self._mcp, self._state)
        return (await self._calls.ask("who")).text

    async def finger(self, user: str) -> str:
        """Inspect a teammate's sessions, availability, and current plan.

        Args:
            user: Space-separated addresses. Use a bare user for all their sessions
                or user:tty to select one session; an @ prefix is optional.

        Returns:
            Presence blocks for matching live sessions. Missing users or sessions
            are described in text; an empty input produces no blocks.

        Raises:
            ValueError: An address is malformed.
            ToolError: biffd is not installed, did not start, or refused the call;
                the message names the cause and the command that repairs it.
        """
        await update_current_session(self._state)
        await refresh_read_messages(self._mcp, self._state)
        for address in user.split():
            parse_address(address)
        return (await self._calls.ask("finger", user=user)).text

    async def last(self, user: str = "", count: int = 25) -> str:
        """Inspect recent login and logout history.

        Args:
            user: Filter by username, optionally prefixed with @. Empty means all users.
            count: Maximum displayed sessions, clamped to the range 1 through 100.

        Returns:
            Formatted session history, marking active sessions as still logged in,
            or 'No session history.' when no events match.

        Raises:
            ToolError: biffd is not installed, did not start, or refused the call;
                the message names the cause and the command that repairs it.
        """
        await update_current_session(self._state)
        return (await self._calls.ask("last", user=user, count=count)).text

    async def read_messages(self) -> str:
        """Return unread durable mail for the current session.

        Call without arguments after an inbox notification.  Mail is counted
        delivered a few seconds after this returns, or at the session's next
        request; a session that dies first is served the mail again.

        Returns:
            Formatted messages or 'No new messages.'.  When the batch left mail
            behind, a last line says how many are still unread.

        Raises:
            ToolError: biffd is not installed, did not start, or refused the call;
                the message names the cause and the command that repairs it.
        """
        # An empty poll is not work: it keeps the presence row alive and no more.
        await self._presence_kept()
        reply = await self._calls.read(self._delivered)
        if reply.carries_mail:
            await self._stamped()
        if reply.more > 0:
            return f"{reply.text}\n{reply.more} more unread; call read_messages again"
        return reply.text

    async def _stamped(self) -> None:
        """Count a read that found mail as activity: mail routing follows it."""
        try:
            await update_current_session(self._state)
        except RELAY_ERRORS as exc:
            logger.warning("read_messages: presence update failed: %s", exc)

    async def _presence_kept(self) -> None:
        """Keep the presence row alive; a relay that cannot is not a failed read."""
        try:
            await get_or_create_session(self._state)
        except RELAY_ERRORS as exc:
            logger.warning("read_messages: presence update failed: %s", exc)

    async def _delivered(self) -> None:
        await refresh_read_messages(self._mcp, self._state)

    async def get_poll_status(self) -> str:
        """Inspect the current polling cadence and the relay's timeout counters.

        Call without arguments to diagnose the running server, not pending config
        changes.  The cadence is this process's configuration; the counters are
        those of the relay biffd holds.

        Returns:
            Whether periodic polling is enabled and its interval.  For a relay that
            counts, the cumulative timeouts and requests when any were made; when
            biffd cannot be asked, the reason instead.
        """
        interval = self._state.config.poll_interval
        if interval <= 0:
            status = "Polling: disabled"
        else:
            if interval >= 60 and interval % 60 == 0:
                shown = f"{interval / 60:g}m"
            else:
                shown = f"{interval:g}s"
            status = f"Polling: active, interval={shown} ({interval:g}s)"
        try:
            reply = await self._calls.ask("status")
        except ToolError as exc:
            return f"{status}\nbiffd: {exc}"
        data = reply.data
        counters = data.get("relay_counters") if isinstance(data, dict) else None
        if isinstance(counters, dict) and counters.get("attempts"):
            status += (
                f"\nNATS relay: {counters['timeouts']} timeout(s) / "
                f"{counters['attempts']} request(s) since server start"
            )
        return status
