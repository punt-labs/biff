"""The ``channel_ack`` tool, answered by biffd: the model's half of a channel check."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Self, final

from biff.server.daemon_calls import DaemonCalls
from biff.server.tools.channel_ack import CHANNEL_ACK_DESCRIPTION

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.mcp_link import McpLink
    from biff.server.state import ServerState

__all__ = ["DaemonChannelAck"]


@final
class DaemonChannelAck:
    """Registers ``channel_ack``, which tells biffd the model saw the check token.

    biffd made the token, accepted the doctor's request and holds the reply
    subject, so it spends the token and answers the doctor; this returns its
    text unchanged.
    """

    NAMES: ClassVar[tuple[str, ...]] = ("channel_ack",)
    """The tool it registers: the in-process tool it replaces."""

    __slots__ = ("_calls", "_mcp")

    _calls: DaemonCalls
    _mcp: FastMCP[ServerState]

    def __new__(
        cls, mcp: FastMCP[ServerState], state: ServerState, link: McpLink
    ) -> Self:
        """Serve the tool of *mcp* for *state*'s sessions through *link*."""
        self = super().__new__(cls)
        self._mcp = mcp
        self._calls = DaemonCalls(state, link)
        return self

    def register(self) -> None:
        """Register ``channel_ack``."""
        self._mcp.tool(name="channel_ack", description=CHANNEL_ACK_DESCRIPTION)(
            self.channel_ack
        )

    async def channel_ack(self, token: str) -> str:
        """Acknowledge a channel check with the token from its channel event.

        Args:
            token: The 32 hex characters the channel check event names.

        Returns:
            ``biff channel live.`` for the pending token; a wrong or repeated
            token changes nothing and says so.
        """
        return (await self._calls.ask("channel_ack", token=token)).text
