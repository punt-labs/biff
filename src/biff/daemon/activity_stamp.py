"""The plugin hook's per-session activity stamp, read by the heartbeat."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self, final

from biff._stdlib import biff_data_dir

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["ActivityStamp"]


@final
class ActivityStamp:
    """The ``<session_id>.active`` file the PostToolUse hook touches per tool call.

    The path is derived here from the session id and the user's biff data
    directory; a client never names it.  The hello's session id has already
    passed the id charset, so it cannot climb out of that directory.
    """

    __slots__ = ("_path",)

    _path: Path

    def __new__(cls, session_id: str) -> Self:
        """Name the stamp of *session_id* in ``~/.punt-labs/biff/unread``."""
        self = super().__new__(cls)
        self._path = biff_data_dir() / "unread" / f"{session_id}.active"
        return self

    def touched_at(self) -> datetime | None:
        """Return when the hook last stamped, or ``None`` if it never has."""
        try:
            return datetime.fromtimestamp(self._path.stat().st_mtime, UTC)
        except FileNotFoundError:
            return None
