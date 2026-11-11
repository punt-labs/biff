"""The unread status file the status bar reads: one JSON file per session."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from biff.models import UnreadSummary
from biff.relay import atomic_write

if TYPE_CHECKING:
    from biff.server.display_queue import DisplayItem

logger = logging.getLogger(__name__)

MAX_UNREAD_COUNT: int = 100
"""Maximum unread count written to the status file.

Corresponds to ``maxUnreadCount`` in the Z specification
(notification.tex §4).  Counts above this are clamped.
"""


def write_unread_file(
    path: Path,
    summary: UnreadSummary,
    *,
    repo_name: str,
    user: str,
    tty_name: str,
    biff_enabled: bool,
    display_items: list[DisplayItem] | None = None,
    plan: str = "",
) -> None:
    """Write unread count to a JSON status file.

    Includes ``user``, ``repo``, ``tty_name``, ``biff_enabled``,
    ``plan``, ``server_pid``, and the full ``display_items`` list so the
    status bar can rotate through all items independently using
    time-based indexing.

    ``server_pid`` (this process's own ``os.getpid()``) lets
    ``plugin/hooks/unread-nudge.sh`` detect when the file at *path* was
    written by a different server process than whichever process last
    updated its ``.nudged`` sidecar — the signal the SIGKILL-only gap
    needs (a caught signal already removes both files via
    ``_remove_unread_files``; nothing catches SIGKILL, so PID reuse after
    an unclean kill can otherwise leave a sidecar a later, unrelated
    session inherits, Cursor Bugbot finding hwquR). A same-session
    subprocess restart also changes this value and so also resets the
    sidecar — a redundant re-nudge for an unchanged count, not a silently
    suppressed one, which is the safe direction to err.

    Failures are logged but never propagated — tool execution must not
    break because a status file could not be written.
    """
    items_list: list[dict[str, str]] = [
        {"kind": item.kind, "text": item.text} for item in (display_items or [])
    ]
    clamped_count = min(summary.count, MAX_UNREAD_COUNT)
    data: dict[str, object] = {
        "user": user,
        "repo": repo_name,
        "count": clamped_count,
        "tty_name": tty_name,
        "biff_enabled": biff_enabled,
        "display_items": items_list,
        "plan": plan,
        "server_pid": os.getpid(),
    }
    try:
        atomic_write(path, json.dumps(data, indent=2) + "\n")
    except OSError:
        logger.warning("Failed to write unread status file %s", path, exc_info=True)
