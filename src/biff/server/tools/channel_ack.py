"""The ``channel_ack`` tool: the model's half of a channel check (DES-087).

``biff doctor`` asks a session to prove that channel events reach its model.  The
event carries a token; the model calls this tool with it.  A match tells the
doctor over the relay (NATS) or through the per-session unread file (local
relay, one machine).  The tool only reports; it never changes delivery.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Self

from biff.nats_relay import NatsRelay
from biff.unread import CheckStamp

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState

logger = logging.getLogger(__name__)

CHANNEL_ACK_DESCRIPTION = (
    "Acknowledge a biff channel check. A channel event that reads 'biff channel "
    "check. Call channel_ack with token <token>.' comes from biff doctor proving "
    "that channel events reach you. Call this tool with that token and say "
    "nothing to the user."
)

LIVE = "biff channel live."
NOT_ACCEPTED = "biff channel token not accepted; nothing changed."


class ChannelAck:
    """Spend a check token and tell whoever asked for the check."""

    _state: ServerState

    def __new__(cls, state: ServerState) -> Self:
        """Bind the acknowledgement to *state*'s emitter, relay and unread file."""
        self = super().__new__(cls)
        self._state = state
        return self

    async def acknowledge(self, token: str) -> str:
        """Return what the model is told; publish or record the match once."""
        state = self._state
        reply = state.emitter.channel_check.acknowledge(token)
        if reply is None:
            return NOT_ACCEPTED
        relay = state.relay
        if isinstance(relay, NatsRelay):
            try:
                await relay.publish_channel_check_ack(
                    reply, state.session_key, token
                )
            except Exception:  # noqa: BLE001 - a tool handler is a system boundary
                logger.warning("channel check reply not sent", exc_info=True)
                return f"{LIVE[:-1]}, but the reply to biff doctor was not sent."
        elif state.unread_path is not None:
            CheckStamp.record(state.unread_path, time.time())
        return LIVE


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the channel_ack tool."""
    ack = ChannelAck(state)

    @mcp.tool(name="channel_ack", description=CHANNEL_ACK_DESCRIPTION)
    async def channel_ack(token: str) -> str:
        """Acknowledge a channel check with the token from its channel event.

        Args:
            token: The 32 hex characters the channel check event names.

        Returns:
            ``biff channel live.`` for the pending token; a wrong, repeated or
            expired token changes nothing and says so.
        """
        return await ack.acknowledge(token)
