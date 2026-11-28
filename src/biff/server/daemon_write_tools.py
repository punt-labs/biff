"""The tools that change something, answered by biffd.


Each tool asks the daemon to run the command and returns the text of its reply
unchanged; what only this process can do (the plan-gate marker, the status-line
file, the tool descriptions) it does around the call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Self, final

from biff.server.daemon_calls import DaemonCalls
from biff.server.tools._activity import track_activity
from biff.server.tools._descriptions import refresh_read_messages
from biff.server.tools._session import update_current_session

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.mcp_link import McpLink
    from biff.server.state import ServerState

__all__ = ["DaemonWriteTools"]

# What each tool says about itself.  The in-process tools say the same, and a test
# fails when the two servers list a tool differently.
WRITE_DESCRIPTION = (
    "Send a message to a teammate. "
    "Delivery is confirmed before this returns; a failure after "
    "one retry is reported explicitly, never silently."
)


@final
class DaemonWriteTools:
    """Registers the write-side tools on a server, backed by one link to biffd."""

    NAMES: ClassVar[tuple[str, ...]] = ("write",)
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
        """Register the write-side tools."""
        guard = track_activity(self._state)
        self._mcp.tool(name="write", description=WRITE_DESCRIPTION)(guard(self.write))

    async def write(self, to: str, message: str) -> str:
        """Deliver a durable message to a teammate.

        Args:
            to: A bare username broadcasts to their shared inbox; user:tty targets
                a session. An @ prefix is optional.
            message: Text split into bounded chunks when needed.

        Returns:
            Confirmed delivery details, an addressing error, or an explicit failure
            after one retry. Delivery failures are not reported as success.

        Raises:
            ToolError: biffd is not installed, did not start, or refused the call;
                the message names the cause and the command that repairs it.
        """
        await update_current_session(self._state)
        await refresh_read_messages(self._mcp, self._state)
        return (await self._calls.ask("write", to=to, message=message)).text
