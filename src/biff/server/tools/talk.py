"""Real-time conversation tools — ``talk``, ``talk_read``, ``talk_end``.

Talk is ephemeral (BSD ``talk``): frames ride NATS core pub/sub with no
durable inbox.  The MCP server holds a shared :class:`~biff.talk_state.TalkState`
that an always-on subscription feeds (``_descriptions.subscribe_talk``);
these tools drive it:

* ``talk`` — accept a pending invite (completing the human's handshake),
  send a message while connected, or send an invite.  All ephemeral.
* ``talk_read`` — drain the held state and return who wants to talk plus
  any queued messages.  This is the tool the model calls after the
  tool-list-changed push (DES-020/021).
* ``talk_end`` — close the conversation (sends an end frame if connected).
* ``talk_listen`` — the blocking variant for agent-to-agent flows.

The accept / invite / send / end decision logic lives once in
:mod:`biff.commands.talk`; these tools wrap each shared action with a
``refresh_talk`` description push and render the returned ``CommandResult``.

Talk is NATS-only.  LocalRelay and DormantRelay return an error message.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import biff.commands.talk as talk_commands
from biff.commands.talk_read import TalkReadout
from biff.nats_relay import NatsRelay
from biff.server.tools._activity import track_activity
from biff.server.tools._descriptions import (
    TALK_BASE_DESCRIPTION,
    refresh_talk,
)
from biff.server.tools._session import update_current_session

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState

logger = logging.getLogger(__name__)


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register talk tools."""

    @mcp.tool(
        name="talk",
        description=TALK_BASE_DESCRIPTION,
        meta={"anthropic/alwaysLoad": True},
    )
    @track_activity(state)
    async def talk(to: str, message: str = "") -> str:
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
            OSError: Local relay storage cannot be accessed.
            ConnectionError: The network relay cannot establish a connection.
            TimeoutError: A relay operation exceeds its deadline.
            nats.errors.Error: A NATS request or connection fails.
        """
        if not isinstance(state.relay, NatsRelay):
            return "Talk requires a NATS relay connection."
        await update_current_session(state)
        result = await talk_commands.talk(state, to, message)
        await refresh_talk(mcp, state)
        return result.text

    @mcp.tool(
        name="talk_read",
        description=(
            "Show pending talk invites and queued talk messages held by the "
            "server, and mark them read. Call this after a talk notification."
        ),
    )
    @track_activity(state)
    async def talk_read() -> str:
        """Read pending talk invitations and consume queued talk messages.

        Call without arguments after a talk notification. Read the server-held
        ephemeral state, never the durable inbox. Include session-specific accept
        hints for pending invitations; consumed messages are not replayed.

        Returns:
            Invitations, messages, and hangups, or 'No pending talk activity.'.
            Unsupported relays and unconfirmed mutual-invite accepts are reported
            in text rather than silently presenting a connected conversation.

        Raises:
            OSError: Local relay storage cannot be accessed.
            ConnectionError: The network relay cannot establish a connection.
            TimeoutError: A relay operation exceeds its deadline.
            nats.errors.Error: A NATS request or connection fails.
        """
        if not isinstance(state.relay, NatsRelay):
            return "Talk requires a NATS relay connection."
        # Reading is not work: replying with `talk` stamps activity, a poll does not.
        readout = await TalkReadout.take(state)
        await refresh_talk(mcp, state)
        return readout.text()

    @mcp.tool(
        name="talk_listen",
        description=(
            "Block until talk activity arrives (agent-to-agent). Human "
            "sessions are prompted to call talk_read by the tool list instead."
        ),
    )
    @track_activity(state)
    async def talk_listen(timeout: int = 30) -> str:
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
            OSError: Local relay storage cannot be accessed.
            ConnectionError: The network relay cannot establish a connection.
            TimeoutError: A relay operation exceeds its deadline.
            nats.errors.Error: A NATS request or connection fails.
        """
        if not isinstance(state.relay, NatsRelay):
            return "Talk requires a NATS relay connection."
        await update_current_session(state)
        # Block until there is new drainable traffic — queued frames or a
        # pending invite.  A bare open connection is not, by itself, activity,
        # so an active conversation with an empty queue keeps waiting for the
        # partner's next frame instead of returning at once.  An arrival wakes
        # the wait; nothing polls.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(timeout)
        while not state.talk.has_pending_traffic and loop.time() < deadline:
            await state.wake.nudged(deadline - loop.time())
        readout = await TalkReadout.take(state)
        await refresh_talk(mcp, state)
        return readout.text()

    @mcp.tool(name="talk_end", description="End the current talk session.")
    @track_activity(state)
    async def talk_end() -> str:
        """End the active conversation or cancel a pending outgoing invitation.

        Call without arguments when finished talking. Send an ephemeral end frame
        when connected and refresh the advertised local conversation state.

        Returns:
            A close/cancel confirmation, a no-active-conversation notice, or a
            relay/publish error reported by the shared talk action.

        Raises:
            OSError: Local relay storage cannot be accessed.
            ConnectionError: The network relay cannot establish a connection.
            TimeoutError: A relay operation exceeds its deadline.
            nats.errors.Error: A NATS request or connection fails.
        """
        await update_current_session(state)
        result = await talk_commands.end_or_cancel(state)
        await refresh_talk(mcp, state)
        return result.text
