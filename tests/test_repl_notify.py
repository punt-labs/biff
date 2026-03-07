"""Tests for REPL notification state (biff.repl_notify)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from biff.models import WallPost
from biff.repl_notify import NotifyState


class TestNotifyState:
    def test_no_change_returns_empty(self) -> None:
        state = NotifyState()
        assert state.check(0, None) == []

    def test_new_messages_singular(self) -> None:
        state = NotifyState()
        lines = state.check(1, None)
        assert len(lines) == 1
        assert "1 new message" in lines[0]
        assert "messages" not in lines[0]  # singular

    def test_new_messages_plural(self) -> None:
        state = NotifyState()
        lines = state.check(3, None)
        assert len(lines) == 1
        assert "3 new messages" in lines[0]

    def test_no_notification_when_count_unchanged(self) -> None:
        state = NotifyState()
        state.check(2, None)
        lines = state.check(2, None)
        assert lines == []

    def test_incremental_messages(self) -> None:
        state = NotifyState()
        state.check(2, None)
        lines = state.check(5, None)
        assert len(lines) == 1
        assert "3 new messages" in lines[0]

    def test_wall_posted(self) -> None:
        state = NotifyState()
        wall = WallPost(
            text="release freeze",
            from_user="kai",
            from_tty="tty1",
            posted_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        lines = state.check(0, wall)
        assert len(lines) == 1
        assert "WALL" in lines[0]
        assert "release freeze" in lines[0]
        assert "@kai" in lines[0]

    def test_wall_no_repeat(self) -> None:
        state = NotifyState()
        wall = WallPost(
            text="freeze",
            from_user="kai",
            from_tty="tty1",
            posted_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        state.check(0, wall)
        lines = state.check(0, wall)
        assert lines == []

    def test_wall_changed(self) -> None:
        state = NotifyState()
        wall1 = WallPost(
            text="freeze",
            from_user="kai",
            from_tty="tty1",
            posted_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        state.check(0, wall1)
        wall2 = WallPost(
            text="unfreeze",
            from_user="kai",
            from_tty="tty1",
            posted_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        lines = state.check(0, wall2)
        assert len(lines) == 1
        assert "unfreeze" in lines[0]

    def test_wall_cleared(self) -> None:
        state = NotifyState()
        wall = WallPost(
            text="freeze",
            from_user="kai",
            from_tty="tty1",
            posted_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        state.check(0, wall)
        lines = state.check(0, None)
        assert len(lines) == 1
        assert "cleared" in lines[0].lower()

    def test_messages_and_wall_simultaneously(self) -> None:
        state = NotifyState()
        wall = WallPost(
            text="deploy freeze",
            from_user="eric",
            from_tty="tty1",
            posted_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=2),
        )
        lines = state.check(2, wall)
        assert len(lines) == 2
        # One message notification, one wall notification
        texts = " ".join(lines)
        assert "new message" in texts
        assert "WALL" in texts

    def test_count_decrease_no_notification(self) -> None:
        """Messages read elsewhere — count decreases, no notification."""
        state = NotifyState()
        state.check(5, None)
        lines = state.check(3, None)
        assert lines == []

    def test_seeded_state_no_initial_notification(self) -> None:
        """Seeding with initial state produces no notifications."""
        state = NotifyState()
        wall = WallPost(
            text="existing",
            from_user="kai",
            from_tty="tty1",
            posted_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        # First check seeds the state.
        state.check(3, wall)
        # Second check: nothing changed.
        lines = state.check(3, wall)
        assert lines == []
