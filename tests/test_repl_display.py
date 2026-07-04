"""Tests for ReplDisplay (biff.repl_display).

Session-scoped display preferences for the REPL — the ``timestamps``
toggle and its ``[HH:MM]`` stamp rendering (biff-4uq).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from biff.repl_display import ReplDisplay


class TestReplDisplay:
    def test_timestamps_off_by_default(self) -> None:
        assert ReplDisplay().show_timestamps is False

    def test_set_timestamps_on(self) -> None:
        d = ReplDisplay()
        d.set_timestamps(on=True)
        assert d.show_timestamps is True

    def test_set_timestamps_off_after_on(self) -> None:
        d = ReplDisplay()
        d.set_timestamps(on=True)
        d.set_timestamps(on=False)
        assert d.show_timestamps is False

    def test_stamp_empty_when_off(self) -> None:
        d = ReplDisplay()
        # Local-attached instant; still empty because timestamps are off.
        assert d.stamp(datetime(2026, 7, 4, 14, 32).astimezone()) == ""

    def test_stamp_local_hhmm_when_on(self) -> None:
        d = ReplDisplay()
        d.set_timestamps(on=True)
        # A naive local time attached to the local zone round-trips unchanged.
        when = datetime(2026, 7, 4, 14, 32).astimezone()
        assert d.stamp(when) == "[14:32] "

    def test_stamp_normalizes_utc_to_local(self) -> None:
        d = ReplDisplay()
        d.set_timestamps(on=True)
        stamp = d.stamp(datetime(2026, 7, 4, 14, 32, tzinfo=UTC))
        # Exact hour depends on the runner's zone; shape must hold.
        assert re.fullmatch(r"\[\d{2}:\d{2}\] ", stamp) is not None
