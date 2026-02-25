"""Integration tests for /wall tool via RecordingClient.

Tests wall post, read, clear, expiry, and replace-on-post behavior
using the LocalRelay (filesystem-backed, in-memory transport).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from biff.models import WallPost
from biff.testing import RecordingClient


class TestWallPostAndRead:
    async def test_post_wall(self, kai: RecordingClient) -> None:
        result = await kai.call("wall", message="release freeze")
        assert "Wall posted" in result
        assert "release freeze" in result

    async def test_read_wall(self, kai: RecordingClient) -> None:
        await kai.call("wall", message="deploy in progress")
        result = await kai.call("wall")
        assert "deploy in progress" in result
        assert "WALL" in result

    async def test_no_active_wall(self, kai: RecordingClient) -> None:
        result = await kai.call("wall")
        assert "No active wall" in result

    async def test_post_with_duration(self, kai: RecordingClient) -> None:
        result = await kai.call("wall", message="sprint freeze", duration="2h")
        assert "Wall posted" in result
        assert "sprint freeze" in result


class TestWallClear:
    async def test_clear_wall(self, kai: RecordingClient) -> None:
        await kai.call("wall", message="temp wall")
        result = await kai.call("wall", clear=True)
        assert "cleared" in result.lower()

        # Verify it's gone
        result = await kai.call("wall")
        assert "No active wall" in result

    async def test_clear_when_no_wall(self, kai: RecordingClient) -> None:
        result = await kai.call("wall", clear=True)
        assert "cleared" in result.lower()


class TestWallReplace:
    async def test_new_replaces_old(self, kai: RecordingClient) -> None:
        await kai.call("wall", message="first wall")
        await kai.call("wall", message="second wall")
        result = await kai.call("wall")
        assert "second wall" in result
        assert "first wall" not in result


class TestWallSenderTty:
    async def test_wall_includes_sender_tty(self, kai: RecordingClient) -> None:
        """Wall output includes the sender's tty name."""
        await kai.call("tty", name="main")
        await kai.call("wall", message="deploy freeze")
        result = await kai.call("wall")
        assert "kai" in result
        assert "main" in result

    async def test_wall_without_tty_omits_parens(self, kai: RecordingClient) -> None:
        """Wall output omits tty when sender has no tty_name set."""
        await kai.call("wall", message="no tty set")
        result = await kai.call("wall")
        assert "kai" in result
        assert "()" not in result


class TestWallCrossUser:
    async def test_wall_visible_to_other_user(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """Wall posted by kai should be visible to eric."""
        await kai.call("wall", message="team standup in 5")
        result = await eric.call("wall")
        assert "team standup in 5" in result

    async def test_eric_clears_kai_wall(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """Any team member can clear the wall."""
        await kai.call("wall", message="temp announcement")
        await eric.call("wall", clear=True)
        result = await kai.call("wall")
        assert "No active wall" in result


class TestWallExpiry:
    async def test_expired_wall_not_returned(
        self, shared_data_dir: object, kai: RecordingClient
    ) -> None:
        """Manually create an expired wall and verify it's not returned."""
        from pathlib import Path

        from biff.relay import atomic_write

        data_dir = Path(str(shared_data_dir))
        now = datetime.now(UTC)
        expired = WallPost(
            text="old wall",
            from_user="kai",
            posted_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),
        )
        data_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(data_dir / "wall.json", expired.model_dump_json() + "\n")

        result = await kai.call("wall")
        assert "No active wall" in result


class TestWallDurationValidation:
    async def test_invalid_duration(self, kai: RecordingClient) -> None:
        result = await kai.call("wall", message="test", duration="abc")
        assert "Unrecognized" in result

    async def test_exceeds_max_duration(self, kai: RecordingClient) -> None:
        result = await kai.call("wall", message="test", duration="10d")
        assert "maximum" in result

    async def test_long_message_truncated(self, kai: RecordingClient) -> None:
        result = await kai.call("wall", message="x" * 250)
        assert "Wall posted" in result

    async def test_whitespace_only_message_treated_as_read(
        self, kai: RecordingClient
    ) -> None:
        """Whitespace-only message should fall through to read mode, not error."""
        result = await kai.call("wall", message="   ")
        assert "No active wall" in result
