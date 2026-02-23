"""Tests for the ActivityTracker state machine."""

from __future__ import annotations

import time

from biff.server.activity import ActivityTracker


class TestActivityTracker:
    """Unit tests for ActivityTracker state transitions."""

    def test_starts_active(self) -> None:
        tracker = ActivityTracker()
        assert not tracker.napping

    def test_idle_seconds_increases(self) -> None:
        tracker = ActivityTracker()
        time.sleep(0.05)
        assert tracker.idle_seconds() >= 0.04

    def test_touch_resets_idle(self) -> None:
        tracker = ActivityTracker()
        time.sleep(0.05)
        tracker.touch()
        assert tracker.idle_seconds() < 0.05

    def test_enter_nap(self) -> None:
        tracker = ActivityTracker()
        tracker.enter_nap()
        assert tracker.napping

    def test_touch_clears_napping(self) -> None:
        tracker = ActivityTracker()
        tracker.enter_nap()
        assert tracker.napping
        tracker.touch()
        assert not tracker.napping

    def test_record_pop_resets_pop_timer(self) -> None:
        tracker = ActivityTracker()
        time.sleep(0.05)
        tracker.record_pop()
        assert tracker.seconds_since_pop() < 0.05

    def test_seconds_since_pop_increases(self) -> None:
        tracker = ActivityTracker()
        time.sleep(0.05)
        assert tracker.seconds_since_pop() >= 0.04

    def test_pop_does_not_clear_napping(self) -> None:
        tracker = ActivityTracker()
        tracker.enter_nap()
        tracker.record_pop()
        assert tracker.napping

    def test_full_nap_wake_cycle(self) -> None:
        """Active → nap → POP → touch → active."""
        tracker = ActivityTracker()
        tracker.enter_nap()
        tracker.record_pop()
        tracker.touch()
        # After touch: no longer napping, idle timer just reset
        assert not tracker.napping
        assert tracker.idle_seconds() < 0.1
