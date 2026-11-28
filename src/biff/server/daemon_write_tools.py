"""The tools that change something, answered by biffd.

Each tool asks the daemon to run the command and returns the text of its reply
unchanged; what only this process can do (the plan-gate marker, the status-line
file, the tool descriptions) it does around the call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal, Self, final

from fastmcp.exceptions import ToolError

from biff._stdlib import expand_bead_id
from biff.markers import clear_wall_marker, write_wall_marker
from biff.models import WallPost
from biff.plan_marker import PlanMarker
from biff.server.daemon_calls import DaemonCalls
from biff.server.tools._activity import track_activity
from biff.server.tools._descriptions import (
    refresh_read_messages,
    refresh_wall,
    set_biff_enabled,
    set_tty_name,
)
from biff.server.tools.wall import WALL_BASE_DESCRIPTION

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from pydantic import JsonValue

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
PLAN_DESCRIPTION = (
    "Set your BSD-style .plan status: a short, ONE-LINE status of "
    "what you're working on right now (the classic Unix finger/.plan "
    "file), visible to teammates via /finger and /who. This is NOT a "
    "task plan, todo list, or step-by-step breakdown — keep it to "
    "around 40 characters. Example: 'refactoring the auth layer' or "
    "'biff-if2: CLI identity fix'."
)
MESG_DESCRIPTION = (
    "Control message reception. Use 'on' to accept messages, 'off' to block them."
)
TTY_DESCRIPTION = "Name the current session. Visible in /who and /finger TTY column."


@final
class DaemonWriteTools:
    """Registers the write-side tools on a server, backed by one link to biffd."""

    NAMES: ClassVar[tuple[str, ...]] = ("write", "plan", "mesg", "tty", "wall")
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
        self._mcp.tool(name="plan", description=PLAN_DESCRIPTION)(guard(self.plan))
        self._mcp.tool(name="mesg", description=MESG_DESCRIPTION)(guard(self.mesg))
        self._mcp.tool(name="tty", description=TTY_DESCRIPTION)(guard(self.tty))
        self._mcp.tool(name="wall", description=WALL_BASE_DESCRIPTION)(guard(self.wall))

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
        await refresh_read_messages(self._mcp, self._state)
        return (await self._calls.ask("write", to=to, message=message)).text

    async def plan(
        self,
        message: str,
        source: Literal["manual", "auto"] = "manual",
    ) -> str:
        """Publish a short one-line status visible in who and finger.

        Use this for what you are working on, not a task list. Bare task identifiers
        are expanded to their titles when the local tracker CLI is available.

        Args:
            message: A short status; empty clears the current plan.
            source: Hooks use 'auto'; explicit user updates use 'manual'. Automatic
                updates cannot overwrite a nonempty manual plan.

        Returns:
            The published plan, a notice preserving a manual plan, or an error
            explaining the maximum length when the expanded message is too long.

        Raises:
            ToolError: biffd is not installed, did not start, or refused the call;
                the message names the cause and the command that repairs it.
        """
        reply = await self._calls.ask(
            "plan", message=expand_bead_id(message), source=source
        )
        data = reply.data
        if isinstance(data, dict) and "error" not in data:
            # The marker is keyed on this repository and session, which biffd
            # does not have; a kept manual plan is written back as it stands.
            PlanMarker.sync(str(data["plan"]), self._state.bound_session_id)
        await refresh_read_messages(self._mcp, self._state)
        return reply.text

    async def mesg(self, *, enabled: bool) -> str:
        """Control whether this session accepts messages.

        Use enabled=True to accept messages or False to block them. This changes
        the session's availability, not repository-wide enablement.

        Returns:
            BSD-style confirmation: 'is y' for enabled or 'is n' for disabled.

        Raises:
            ToolError: biffd is not installed, did not start, or refused the call;
                the message names the cause and the command that repairs it.
        """
        reply = await self._calls.ask("mesg", value="y" if enabled else "n")
        set_biff_enabled(enabled=enabled)
        await refresh_read_messages(self._mcp, self._state)
        return reply.text

    async def tty(self, name: str = "") -> str:
        """Name the current session for use in teammate addresses.

        Args:
            name: A unique name of at most 20 characters. An empty value claims
                the next available ttyN name.

        Returns:
            'TTY: <name>' on success, or an error for an invalid, occupied, or
            unclaimable name. Successful renames update presence and talk hints.

        Raises:
            ToolError: biffd is not installed, did not start, or refused the call;
                the message names the cause and the command that repairs it.
        """
        reply = await self._calls.ask("tty", name=name)
        data = reply.data
        if isinstance(data, dict) and "tty" in data:
            # Talk invite hints and the status line render the name from here.
            claimed = str(data["tty"])
            set_tty_name(claimed)
            self._state.talk.set_tty_name(claimed)
        await refresh_read_messages(self._mcp, self._state)
        return reply.text

    async def wall(
        self,
        message: str = "",
        duration: str = "",
        *,
        clear: bool = False,
        repo: str = "",
    ) -> str:
        """Post, inspect, or clear a time-limited team broadcast.

        Call without arguments to read the current wall; supply message to post,
        or clear=True to remove it. Posts and clears update the local display
        immediately and are written to the relay before this returns.

        Args:
            duration: A duration such as '30m' or '2h'; empty defaults to one hour.
            repo: A visible repository slug, such as punt-labs__vox. Empty sends
                posts and clears to all visible repositories.

        Returns:
            The active wall or a post/clear confirmation. Invalid durations or
            unauthorized repositories are reported in text, and so is a repository
            the relay refused. Messages are sanitized and limited to 512 characters.

        Raises:
            ToolError: biffd is not installed, did not start, or refused the call;
                the message names the cause and the command that repairs it.
        """
        try:
            reply = await self._calls.ask(
                "wall", message=message, duration=duration, clear=clear, repo=repo
            )
        except ToolError:
            await self._shown_as_held()
            raise
        data = reply.data
        if reply.error or not isinstance(data, dict):
            await self._shown_as_held()
            return reply.text
        # The display, the SessionStart marker and the spoken wall are this
        # process's: biffd has none of them.
        if clear or message.strip() or "text" not in data:
            await self._show_wall(data)
        return reply.text

    async def _shown_as_held(self) -> None:
        """Show the wall the relay holds, after a write that may have half landed.

        A post or clear that failed for one repository may have changed another's,
        and only the relay knows which; if biffd cannot be asked either, the
        display stays as it was.
        """
        try:
            held = (await self._calls.ask("wall")).data
        except ToolError:
            return
        if isinstance(held, dict):
            await self._show_wall(held)

    async def _show_wall(self, data: dict[str, JsonValue]) -> None:
        """Show *data*, a wall or the news that there is none, here."""
        root = str(self._state.repo_common_root)
        if "text" not in data:
            await refresh_wall(self._mcp, self._state, wall=None)
            clear_wall_marker(root)
            return
        post = WallPost.model_validate(data)
        await refresh_wall(self._mcp, self._state, wall=post)
        write_wall_marker(root, post.text, post.expires_at)
