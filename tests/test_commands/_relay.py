"""Test relay with in-memory wtmp for ``last`` command tests.

``LocalRelay`` intentionally skips wtmp persistence. This subclass
adds in-memory storage so command tests can exercise the full
``last`` code path without NATS.
"""

from __future__ import annotations

from pathlib import Path

from biff.models import SessionEvent
from biff.relay import LocalRelay


class WtmpRelay(LocalRelay):
    """LocalRelay extended with in-memory wtmp storage."""

    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)
        self._wtmp: list[SessionEvent] = []

    async def append_wtmp(self, event: SessionEvent) -> None:
        self._wtmp.append(event)

    async def get_wtmp(
        self,
        *,
        user: str | None = None,
        count: int = 25,
    ) -> list[SessionEvent]:
        events = self._wtmp
        if user:
            events = [e for e in events if e.user == user]
        # Most recent first, like the real relay
        return sorted(events, key=lambda e: e.timestamp, reverse=True)[:count]
