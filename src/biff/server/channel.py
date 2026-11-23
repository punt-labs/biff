"""Channel mode: the live stdio connection and its one send capability (DES-087)."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping, MutableMapping
from typing import TYPE_CHECKING

import anyio

if TYPE_CHECKING:
    from mcp.server.connection import Connection

logger = logging.getLogger(__name__)

CHANNEL_MODE_VARIABLE = "BIFF_CHANNEL_MODE"
CHANNEL_CAPABILITY = "claude/channel"
CHANNEL_METHOD = "notifications/claude/channel"

CHANNEL_SEND_TIMEOUT = 3.0


class ChannelUnavailableError(Exception):
    """No live client connection can carry a channel event."""


def take_channel_mode(
    environ: MutableMapping[str, str] | None = None,
) -> bool:
    """Read the channel-mode variable once and remove it from the environment.

    The Claude Code plugin sets it in the ``tty`` server's ``env``; no user
    does.  Removing it keeps child processes (``git``, a nested ``biff``) from
    inheriting a mode that belongs to this process alone.
    """
    env = os.environ if environ is None else environ
    return env.pop(CHANNEL_MODE_VARIABLE, None) == "1"


class ChannelLink:
    """The server's live client connection, narrowed to sending channel events.

    ``serve_channel_stdio`` binds the connection for the life of one stdio
    session.  The MCP SDK's ``Connection.notify`` drops a notification on a
    closed stream without raising, so this sends on the connection's outbound
    directly: a closed stream raises ``ChannelUnavailableError`` and a blocked
    write raises ``TimeoutError``.
    """

    def __init__(self) -> None:
        self._connection: Connection | None = None

    @property
    def is_bound(self) -> bool:
        """Whether a client connection is currently attached."""
        return self._connection is not None

    def bind(self, connection: Connection) -> None:
        """Attach the live connection."""
        self._connection = connection

    def unbind(self) -> None:
        """Detach the connection once its stream is closed."""
        self._connection = None

    async def send(self, content: str, meta: Mapping[str, str]) -> None:
        """Send one ``notifications/claude/channel`` event.

        Raises:
            ChannelUnavailableError: No connection is attached, or its stream
                is closed.
            TimeoutError: The write did not complete within
                ``CHANNEL_SEND_TIMEOUT`` seconds.
        """
        connection = self._connection
        if connection is None:
            msg = "no client connection is attached"
            raise ChannelUnavailableError(msg)
        params = {"content": content, "meta": dict(meta)}
        try:
            async with asyncio.timeout(CHANNEL_SEND_TIMEOUT):
                await connection.outbound.notify(CHANNEL_METHOD, params)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError) as exc:
            msg = "the client connection is closed"
            raise ChannelUnavailableError(msg) from exc
