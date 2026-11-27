"""Carry the plugin hook's tool-call stamp into the shared presence row."""

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
    :meth:`heartbeat` refreshes liveness, then writes the stamp's time to
    ``last_tool_at`` only when it is newer, so a stale stamp never undoes a
    later biff call.  A missing stamp or row is a no-op.
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
        """Refresh ``last_active``, then carry the stamp into ``last_tool_at``."""
        await self._relay.heartbeat(self._session_key)
        touched = self._touched_at()
        if touched is None:
            return
        session = await self._relay.get_session(self._session_key)
        if session is not None and touched > session.last_tool_at:
            await self._relay.update_session(
                session.model_copy(update={"last_tool_at": touched})
            )

    def _touched_at(self) -> datetime | None:
        if self._stamp is None:
            return None
        try:
            return datetime.fromtimestamp(self._stamp.stat().st_mtime, UTC)
        except FileNotFoundError:
            return None
