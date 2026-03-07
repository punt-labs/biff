"""REPL notification state — tracks changes for inline display.

Compares current unread count and wall state with the previous
snapshot. Returns notification lines to print before the prompt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from biff.formatting import format_remaining
from biff.models import WallPost

logger = logging.getLogger(__name__)


@dataclass
class NotifyState:
    """Mutable state for between-command notification checks."""

    last_unread: int = 0
    last_wall_text: str = ""

    def check(self, unread: int, wall: WallPost | None) -> list[str]:
        """Compare current state with previous and return notification lines.

        Each returned string is a complete notification line ready to print.
        Returns an empty list when nothing changed.
        """
        lines: list[str] = []

        # New messages since last check.
        if unread > self.last_unread:
            delta = unread - self.last_unread
            plural = "s" if delta != 1 else ""
            lines.append(f"  \033[33m📬 {delta} new message{plural}\033[0m")
        elif unread == 0 and self.last_unread > 0:
            # Went from unread to caught up (someone read elsewhere).
            pass

        # Wall changes.
        wall_text = wall.text if wall else ""
        if wall_text and wall_text != self.last_wall_text:
            remaining = format_remaining(wall.expires_at) if wall else ""
            from_user = wall.from_user if wall else ""
            lines.append(
                f"  \033[1;31m📢 WALL @{from_user}: {wall_text} ({remaining})\033[0m"
            )
        elif not wall_text and self.last_wall_text:
            lines.append("  \033[2m📢 Wall cleared\033[0m")

        # Update state.
        self.last_unread = unread
        self.last_wall_text = wall_text

        return lines

    def sync(self, unread: int, wall: WallPost | None) -> None:
        """Update state without generating notifications.

        Call after the user's own command changes state (e.g., posting
        a wall or reading messages) to prevent self-notification on the
        next poll.
        """
        self.last_unread = unread
        self.last_wall_text = wall.text if wall else ""
