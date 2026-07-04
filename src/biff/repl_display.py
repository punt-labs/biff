"""Session-scoped REPL display preferences.

Preferences toggled at the REPL prompt (e.g. ``timestamps on``) that affect
how output is rendered.  These are display state, not configuration — they
live only for the duration of the interactive session and are never
persisted (biff-4uq).
"""

from __future__ import annotations

from datetime import datetime
from typing import Self


class ReplDisplay:
    """Mutable display preferences for one interactive REPL session.

    A single instance is created in ``_repl`` and threaded through the loop
    and talk mode.  ``show_timestamps`` starts off, matching the historical
    default of timestamp-free talk output.
    """

    __slots__ = ("_show_timestamps",)

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._show_timestamps = False
        return self

    @property
    def show_timestamps(self) -> bool:
        """Whether talk output is prefixed with a local ``[HH:MM]`` stamp."""
        return self._show_timestamps

    def set_timestamps(self, *, on: bool) -> None:
        """Enable or disable inline timestamps."""
        self._show_timestamps = on

    def stamp(self, when: datetime) -> str:
        """Render a local ``[HH:MM] `` prefix, or ``""`` when timestamps are off.

        *when* is the instant to render (the caller passes the current time
        per message); it is normalized to the local timezone before
        formatting.  Kept as a parameter so the renderer is a pure function
        of its input and can be tested deterministically.
        """
        if not self._show_timestamps:
            return ""
        return f"[{when.astimezone().strftime('%H:%M')}] "
