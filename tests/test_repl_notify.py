"""Tests for REPL notification state (biff.repl_notify)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from biff._formatting import TABLE_WIDTH, visible_width
from biff.formatting import _NO_PRINTABLE_TEXT
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
        assert "kai" in lines[0]

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

    def test_wall_escapes_neutralized(self) -> None:
        """A malicious wall can't inject terminal escapes into the banner (biff-lbj)."""
        state = NotifyState()
        wall = WallPost(
            from_user="ev\x1b[2Kil",
            text="dep\x1b[2Jloy freeze",
            posted_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        lines = state.check(0, wall)
        assert len(lines) == 1
        # Our own color codes remain; the injected screen-clear/line-erase do not.
        assert "\x1b[2J" not in lines[0]
        assert "\x1b[2K" not in lines[0]
        assert "dep[2Jloy freeze" in lines[0]

    def test_wall_cjk_body_wraps_within_table_width(self) -> None:
        """A CJK/emoji-heavy wall post wraps instead of overflowing 80 columns.

        Mirrors ``TestFormatWallStatusLine.test_cjk_body_wraps_within_the_table_width``
        — the between-prompt banner shares the same up-to-512-char wall body
        as ``format_wall_status_line``, and was the last unwrapped render site
        left standing (biff-2sw).
        """
        state = NotifyState()
        wall = WallPost(
            text="这是一段很长的中文文本用来测试自动换行是否正常工作" * 3,
            from_user="kai",
            from_tty="tty1",
            posted_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        lines = state.check(0, wall)
        assert len(lines) == 1
        rendered_lines = lines[0].splitlines()
        assert len(rendered_lines) > 1
        for rendered_line in rendered_lines:
            assert visible_width(rendered_line) <= TABLE_WIDTH

    def test_wall_control_only_body_renders_fallback(self) -> None:
        """A control-only wall post shows the explicit fallback, not silence.

        Mirrors ``TestFormatWallStatusLine.test_control_only_body_renders_fallback``
        — before this fix, ``terminal_safe`` stripped the body to nothing and
        the banner rendered as ``WALL kai: (1h remaining)`` with no hint the
        poster's message was dropped.
        """
        state = NotifyState()
        wall = WallPost(
            text="\x00\x1b\x07",
            from_user="kai",
            from_tty="tty1",
            posted_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        lines = state.check(0, wall)
        assert len(lines) == 1
        assert _NO_PRINTABLE_TEXT in lines[0]

    def test_wall_giant_sender_renders_bounded(self) -> None:
        """A forged 10,000-char sender must not blow up the between-prompt banner.

        Mirrors ``TestFormatWall.test_giant_sender_renders_bounded`` in
        tests/test_formatting.py — ``_format_wall_banner`` was the third,
        independently vulnerable call site for the same defect: unlike
        ``format_wall`` and ``format_wall_status_line``, it was never wired
        to the ``_MAX_LABEL_WIDTH`` cap, so ``from_user`` fed straight into
        the wrap-width budget uncapped, collapsing ``width`` toward 1 and
        exploding a 500-char body into ~400 hard-broken lines, each
        carrying a 10,000-char indent — ~4,000,000 characters of REPL
        output between prompts from a single forged sender.
        """
        state = NotifyState()
        wall = WallPost(
            text="word " * 100,  # 500 chars — WallPost caps text at 512
            from_user="u" * 10_000,
            posted_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        lines = state.check(0, wall)
        assert len(lines) == 1
        rendered_lines = lines[0].splitlines()
        assert len(rendered_lines) <= 40
        longest = max(visible_width(line) for line in rendered_lines)
        assert longest <= 2 * TABLE_WIDTH

    def test_wall_unbroken_body_suffix_never_overflows_table_width(self) -> None:
        """An unbroken body's suffix must not push the last line past width.

        Mirrors ``TestFormatWallStatusLine.
        test_unbroken_body_suffix_never_overflows_table_width`` in
        tests/test_formatting.py — ``_format_wall_banner`` appended the
        ``(remaining)`` suffix to the last wrapped chunk *after*
        ``wrap_cells`` had already chosen line breaks, so a body with no
        spaces to break at could hard-wrap to a last chunk that exactly
        filled the wrap budget; the suffix then overflowed ``TABLE_WIDTH``.
        """
        state = NotifyState()
        prefix_width = visible_width("  \U0001f4e2 WALL kai: ")
        body_width = TABLE_WIDTH - prefix_width
        wall = WallPost(
            text="x" * (body_width * 3),
            from_user="kai",
            posted_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        lines = state.check(0, wall)
        assert len(lines) == 1
        for rendered_line in lines[0].splitlines():
            assert visible_width(rendered_line) <= TABLE_WIDTH
