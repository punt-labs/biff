"""Hand the plugin hook's tool-call stamp to the relay heartbeat."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from biff.relay import Relay


class HookActivity:
    """The heartbeat of one session, with the hook's tool-call stamp.

    The PostToolUse hook touches the stamp (the unread file's ``.active``
    sibling) on every tool call, biff's own or not, so an agent working in
    Bash and Edit for hours still reads as active to every other host.
    :meth:`heartbeat` passes the stamp's time to the relay heartbeat, which
    advances ``last_tool_at`` inside its own read-modify-write, never backwards.
    There is no second write to race a concurrent ``/plan``.
    """

    _relay: Relay
    _session_key: str
    _stamp: Path | None

    def __new__(cls, relay: Relay, session_key: str, unread_path: Path | None) -> Self:
        """Bind the row *session_key* in *relay* to *unread_path*'s stamp."""
        self = super().__new__(cls)
        self._relay = relay
        self._session_key = session_key
        stamp = unread_path and unread_path.with_suffix(".active")
        self._stamp = stamp
        return self

    async def heartbeat(self) -> None:
        """Refresh ``last_active`` and carry the stamp into ``last_tool_at``."""
        await self._relay.heartbeat(self._session_key, self._touched_at())

    def _touched_at(self) -> datetime | None:
        if self._stamp is None:
            return None
        try:
            return datetime.fromtimestamp(self._stamp.stat().st_mtime, UTC)
        except FileNotFoundError:
            return None
