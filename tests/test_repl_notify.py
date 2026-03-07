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

    def test_sync_sets_snapshot_without_notifications(self) -> None:
        """Partition 25: sync() updates snapshot, check() returns empty."""
        state = NotifyState()
        wall = WallPost(
            text="freeze",
            from_user="kai",
            from_tty="tty1",
            posted_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        state.sync(5, wall)
        assert state.last_unread == 5
        assert state.last_wall_key != ""
        # Next check with same values → no notifications.
        lines = state.check(5, wall)
        assert lines == []

    def test_sync_at_zero_unread(self) -> None:
        """Partition 26: boundary unread=0 after sync."""
        state = NotifyState()
        state.sync(0, None)
        assert state.last_unread == 0
        lines = state.check(0, None)
        assert lines == []

    def test_sync_at_max_unread(self) -> None:
        """Partition 27: boundary unread=100 after sync."""
        state = NotifyState()
        state.sync(100, None)
        assert state.last_unread == 100
        lines = state.check(100, None)
        assert lines == []

    def test_sync_wall_key_updated(self) -> None:
        """Partition 28: sync updates wall key, no re-notification."""
        state = NotifyState()
        wall = WallPost(
            text="deploy",
            from_user="eric",
            from_tty="tty2",
            posted_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        state.sync(0, wall)
        lines = state.check(0, wall)
        assert lines == []

    def test_poll_count_decreased_no_notification(self) -> None:
        """Partition 43: unread decreases → no notification."""
        state = NotifyState()
        state.check(5, None)
        lines = state.check(3, None)
        assert lines == []
        assert state.last_unread == 3  # Updated silently

    def test_poll_count_same_no_notification(self) -> None:
        """Partition 44: unread same → no notification."""
        state = NotifyState()
        state.check(5, None)
        lines = state.check(5, None)
        assert lines == []

    def test_poll_new_messages_boundary_0_to_1(self) -> None:
        """Partition 40: first message (0→1)."""
        state = NotifyState()
        lines = state.check(1, None)
        assert len(lines) == 1
        assert "1 new message" in lines[0]
        assert "messages" not in lines[0]  # Singular

    def test_poll_new_messages_boundary_0_to_100(self) -> None:
        """Partition 41: 0→100 (maxUnread boundary)."""
        state = NotifyState()
        lines = state.check(100, None)
        assert len(lines) == 1
        assert "100 new messages" in lines[0]

    def test_poll_new_messages_incremental(self) -> None:
        """Partition 42: 3→5 (delta=2)."""
        state = NotifyState()
        state.check(3, None)
        lines = state.check(5, None)
        assert len(lines) == 1
        assert "2 new messages" in lines[0]

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
