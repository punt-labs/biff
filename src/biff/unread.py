"""Shared unread-state types and readers.

Extracted from ``statusline.py`` so multiple consumers can parse
session unread files without duplication.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from biff.changed_file import ChangedFile


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
    repo: str = ""
    biff_enabled: bool = True
    display_items: tuple[DisplayItemView, ...] = ()
    plan: str = ""
    channel_checked_at: float | None = None  # absent until a check is acknowledged


class CheckStamp:
    """The ``channel_checked_at`` field: when the model last acknowledged a check.

    Only the local relay writes it: the session and ``biff doctor`` share a
    machine there, so the file is their meeting point.  Every later rewrite of
    the file carries it forward.
    """

    KEY = "channel_checked_at"

    @classmethod
    def value_in(cls, data: dict[str, object]) -> float | None:
        """Return the stamp held in parsed file data, or ``None`` if it has none."""
        raw = data.get(cls.KEY)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        return float(raw)

    @classmethod
    def read(cls, path: Path) -> float | None:
        """Return the stamp in the file at *path*; ``None`` if absent or unreadable."""
        try:
            return cls.value_in(as_str_dict(json.loads(path.read_text())))
        except (OSError, json.JSONDecodeError):
            return None

    @classmethod
    def record(cls, path: Path, at: float) -> None:
        """Write *at* into the file at *path*, keeping every other field."""
        try:
            data = as_str_dict(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):
            data = {}
        data[cls.KEY] = at
        ChangedFile.write(path, json.dumps(data, indent=2) + "\n")


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
        parsed: object = json.loads(path.read_text())
        data = as_str_dict(parsed)
        if not data:
            return None
        items = parse_display_items(data.get("display_items"))
        count_raw = data.get("count", 0)
        count = int(count_raw) if isinstance(count_raw, (int, float)) else 0
        return SessionUnread(
            user=str(data.get("user", "")),
            count=count,
            tty_name=str(data.get("tty_name", "")),
            repo=str(data.get("repo", "")),
            biff_enabled=bool(data.get("biff_enabled", True)),
            display_items=tuple(items),
            plan=str(data.get("plan", "")),
            channel_checked_at=CheckStamp.value_in(data),
        )
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return None
