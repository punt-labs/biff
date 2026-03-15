"""Shared unread-state types and readers.

Extracted from ``statusline.py`` so both the status line and the
lux applet can parse session unread files without duplication.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast


def as_str_dict(val: object) -> dict[str, object]:
    """Narrow an opaque value to ``dict[str, object]``.

    JSON dicts always have string keys, so this is a safe narrowing
    after ``json.loads`` / ``dict.get()`` on parsed session data.
    """
    if isinstance(val, dict):
        return cast("dict[str, object]", val)
    return {}


@dataclass(frozen=True)
class DisplayItemView:
    """A display item as read from the unread status file."""

    kind: str
    text: str


@dataclass(frozen=True)
class SessionUnread:
    """Unread state for a single session, parsed from a PPID-keyed file."""

    user: str
    count: int
    tty_name: str
    biff_enabled: bool = True
    display_items: tuple[DisplayItemView, ...] = ()


def parse_display_items(val: object) -> list[DisplayItemView]:
    """Parse the ``display_items`` array from the unread status file.

    Accepts the raw JSON value and returns a typed list, silently
    skipping malformed entries.
    """
    if not isinstance(val, list):
        return []
    result: list[DisplayItemView] = []
    for raw in cast("list[object]", val):
        if isinstance(raw, dict):
            d = cast("dict[str, object]", raw)
            result.append(
                DisplayItemView(
                    kind=str(d.get("kind", "")),
                    text=str(d.get("text", "")),
                )
            )
    return result


def read_session_unread(path: Path) -> SessionUnread | None:
    """Read a PPID-keyed unread file, returning ``None`` on any error."""
    try:
        data = as_str_dict(json.loads(path.read_text()))
        items = parse_display_items(data.get("display_items"))
        count_raw = data.get("count", 0)
        count = int(count_raw) if isinstance(count_raw, (int, float)) else 0
        return SessionUnread(
            user=str(data.get("user", "")),
            count=count,
            tty_name=str(data.get("tty_name", "")),
            biff_enabled=bool(data.get("biff_enabled", True)),
            display_items=tuple(items),
        )
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return None
