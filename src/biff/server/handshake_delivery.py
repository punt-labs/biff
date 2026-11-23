"""The legacy ``tools/list_changed`` notification debt, kept under one lock."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mcp.types import ToolListChangedNotification
from mcp.types.version import MODERN_PROTOCOL_VERSIONS

from biff.server.tool_list_send import ListChanged

if TYPE_CHECKING:
    from mcp.server.session import ServerSession
    from mcp.server.subscriptions import SubscriptionBus

    from biff.server.channel import ChannelLink

_NOTIFY_SEND_TIMEOUT = 3.0
_OWED_RECHECK = 1.0


@dataclass
class HandshakeDelivery:
    """Track legacy notification debt under one lock (DES-061)."""

    _session: ServerSession | None = None
    _pending_notify: bool = False
    # Channel mode serves one connection for the process's life: a failed send
    # keeps its session, and a write the SDK dropped stays owed (DES-087).
    _link: ChannelLink | None = None
    # Hold across send and debt update so a concurrent success cannot erase a drop.
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def capture_session(self, session: ServerSession) -> None:
        """Capture the handshake session and flush any notification owed."""
        if session.protocol_version in MODERN_PROTOCOL_VERSIONS:
            return
        async with self._lock:
            self._session = session
            if self._pending_notify:
                await self._notify_standalone()

    async def notify(self, bus: SubscriptionBus | None = None) -> None:
        """Publish to modern subscribers and notify the handshake client.

        Args:
            bus: Server event bus, or None to send the handshake notification only.
        """
        from fastmcp.server.dependencies import get_context  # noqa: PLC0415

        try:
            ctx = get_context()
        except RuntimeError:
            ctx = None
        legacy = (
            ctx is None or ctx.session.protocol_version not in MODERN_PROTOCOL_VERSIONS
        )
        # Owe the change before the first await: a cancellation at the bus
        # checkpoint must not leave a changed description with no debt.
        self._pending_notify = self._pending_notify or legacy
        if bus is not None:
            await ListChanged(_NOTIFY_SEND_TIMEOUT, None).publish(bus)

        if ctx is None:
            async with self._lock:
                await self._notify_standalone()
        elif legacy:
            async with self._lock:
                sent = await ListChanged(_NOTIFY_SEND_TIMEOUT, self._link).send(
                    lambda: ctx.send_notification(ToolListChangedNotification())
                )
                self._pending_notify = not sent.delivered
                self._session = ctx.session

    async def flush_owed(self) -> None:
        """Retry a notification a single connection still owes (DES-087)."""
        async with self._lock:
            if (
                self._link
                and self._pending_notify
                and not self._link.seconds_until_ready()
            ):
                await self._notify_standalone()

    def seconds_until_retry(self) -> float | None:
        """Seconds until an owed notification may be retried; ``None`` if none is.

        The poller arms one one-shot wake for it (DES-087), so a debt on a
        quiet relay is retried when the link's back-off ends, not at the next
        liveness check.  A debt that is owed and retryable but cannot be sent
        yet (no session captured inside the handshake window) is looked at again
        after ``_OWED_RECHECK`` seconds.
        """
        if not (self._link and self._pending_notify):
            return None
        return self._link.seconds_until_ready() or _OWED_RECHECK

    def serve_single_connection(self, link: ChannelLink) -> None:
        """Serve one connection for the process's life, observed through *link*.

        The connection starts with no stored session; ``initialize`` captures it.
        """
        self._session, self._link = None, link

    async def _notify_standalone(self) -> None:
        self._pending_notify = True
        push = self._session.send_tool_list_changed if self._session else None
        sent = await ListChanged(_NOTIFY_SEND_TIMEOUT, self._link).send(push)
        self._pending_notify = not sent.delivered
        self._session = self._session if sent.keep_session else None
