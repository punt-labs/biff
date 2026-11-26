"""The ``channel_checked_at`` field of the per-session unread file (DES-087)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from pathlib import Path


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
            parsed: object = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None
        return cls.value_in(cast("dict[str, object]", parsed))

    @classmethod
    def carried(cls, path: Path) -> dict[str, float]:
        """Return the stamp as a field to copy into a rewrite of *path*, or ``{}``."""
        stamp = cls.read(path)
        return {} if stamp is None else {cls.KEY: stamp}
