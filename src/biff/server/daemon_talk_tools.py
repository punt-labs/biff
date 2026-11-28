"""The talk tools, answered by biffd: talk, talk_read, talk_listen and talk_end.

biffd owns the conversation: its queue of invites and lines, and the one
subscription that fills it.  This process mirrors only what the tool descriptions
and the status line need, so after each call it empties its own held talk state
rather than leave a ``[TALK]`` marker for mail biffd already handed over.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, ClassVar, Final, Self, final

from biff.commands.talk_read import TalkReadout
from biff.server.daemon_calls import DaemonCalls
from biff.server.tools._activity import track_activity
from biff.server.tools._descriptions import TALK_BASE_DESCRIPTION, refresh_talk
from biff.server.tools._session import update_current_session

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.mcp_link import McpLink, Reply
    from biff.server.state import ServerState

__all__ = ["DaemonTalkTools"]

TALK_READ_DESCRIPTION = (
    "Show pending talk invites and queued talk messages held by the "
    "server, and mark them read. Call this after a talk notification."
)
TALK_LISTEN_DESCRIPTION = (
    "Block until talk activity arrives (agent-to-agent). Human "
    "sessions are prompted to call talk_read by the tool list instead."
)
TALK_END_DESCRIPTION = "End the current talk session."

_LISTEN_EVERY: Final = 0.25
"""Seconds between looks at biffd while a listen waits for traffic."""


@final
class DaemonTalkTools:
    """Registers the talk tools on a server, backed by one link to biffd."""

    NAMES: ClassVar[tuple[str, ...]] = ("talk", "talk_read", "talk_listen", "talk_end")
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
        """Register the four talk tools."""
        guard = track_activity(self._state)
        self._mcp.tool(
            name="talk",
            description=TALK_BASE_DESCRIPTION,
            meta={"anthropic/alwaysLoad": True},
        )(guard(self.talk))
        self._mcp.tool(name="talk_read", description=TALK_READ_DESCRIPTION)(
            guard(self.talk_read)
        )
        self._mcp.tool(name="talk_listen", description=TALK_LISTEN_DESCRIPTION)(
            guard(self.talk_listen)
        )
        self._mcp.tool(name="talk_end", description=TALK_END_DESCRIPTION)(
            guard(self.talk_end)
        )

    async def talk(self, to: str, message: str = "") -> str:
        """Invite a teammate, accept their invitation, or send a talk message.

        Use this for ephemeral conversation rather than durable inbox delivery.
        An existing invitation from the recipient is accepted; an active connection
        sends the message; otherwise an invitation is sent. Use talk_read for replies.

        Args:
            to: A username or user:tty address, optionally prefixed with @. Use a
                specific session when a username has multiple possible targets.
            message: Optional opening line or a line to send on an active conversation.

        Returns:
            An invitation, acceptance, or send confirmation. Unsupported relays,
            invalid or ambiguous targets, and publish failures are reported in text.

        Raises:
            ToolError: biffd is not installed, did not start, or refused the call;
                the message names the cause and the command that repairs it.
        """
        await update_current_session(self._state)
        return await self._shown(
            await self._calls.ask("talk", to=to, message=message)
        )

    async def talk_read(self) -> str:
        """Read pending talk invitations and consume queued talk messages.

        Call without arguments after a talk notification. Read the daemon-held
        ephemeral state, never the durable inbox. Include session-specific accept
        hints for pending invitations; consumed messages are not replayed.

        Returns:
            Invitations, messages, and hangups, or 'No pending talk activity.'.
            Unsupported relays and unconfirmed mutual-invite accepts are reported
            in text rather than silently presenting a connected conversation.

        Raises:
            ToolError: biffd is not installed, did not start, or refused the call;
                the message names the cause and the command that repairs it.
        """
        # Reading is not work: replying with `talk` stamps activity, a poll does not.
        return await self._shown(await self._calls.ask("talk_read"))

    async def talk_listen(self, timeout: int = 30) -> str:
        """Wait for talk traffic before consuming the pending state.

        Use this in agent-to-agent flows instead of repeatedly calling talk_read.
        An open but empty conversation does not count as pending traffic.

        Args:
            timeout: Maximum wait in seconds; nonpositive values drain immediately.

        Returns:
            The same invitation/message/hangup view as talk_read. An elapsed timeout
            returns the empty-activity notice, not an exception. Unsupported relays
            and unconfirmed mutual-invite accepts are reported in text.

        Raises:
            ToolError: biffd is not installed, did not start, or refused the call;
                the message names the cause and the command that repairs it.
        """
        await update_current_session(self._state)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(timeout)
        while True:
            reply = await self._calls.ask("talk_read")
            if reply.error or reply.text != TalkReadout.NO_ACTIVITY:
                return await self._shown(reply)
            if loop.time() >= deadline:
                return await self._shown(reply)
            await asyncio.sleep(min(_LISTEN_EVERY, deadline - loop.time()))

    async def talk_end(self) -> str:
        """End the active conversation or cancel a pending outgoing invitation.

        Call without arguments when finished talking. Send an ephemeral end frame
        when connected and refresh the advertised local conversation state.

        Returns:
            A close/cancel confirmation, a no-active-conversation notice, or a
            relay/publish error reported by the shared talk action.

        Raises:
            ToolError: biffd is not installed, did not start, or refused the call;
                the message names the cause and the command that repairs it.
        """
        await update_current_session(self._state)
        return await self._shown(await self._calls.ask("talk_end"))

    async def _shown(self, reply: Reply) -> str:
        """Return *reply*'s text after emptying this process's mirror of the talk."""
        self._state.talk.drain_for_agent()
        await refresh_talk(self._mcp, self._state)
        return reply.text
