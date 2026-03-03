"""Tests for ``biff.commands.wall``."""

from __future__ import annotations

from typing import cast

from biff.cli_session import CliContext
from biff.commands.wall import wall
from biff.models import BiffConfig
from biff.relay import LocalRelay


class TestWall:
    async def test_no_active_wall(self, ctx: CliContext) -> None:
        result = await wall(ctx, "", "", clear=False)
        assert not result.error
        assert result.text == "No active wall."
        assert result.json_data is None

    async def test_post_wall(self, ctx: CliContext) -> None:
        result = await wall(ctx, "deploy freeze", "1h", clear=False)
        assert not result.error
        assert "Wall posted" in result.text
        assert "deploy freeze" in result.text
        data = cast("dict[str, object]", result.json_data)
        assert data["text"] == "deploy freeze"

    async def test_read_active_wall(self, ctx: CliContext, relay: LocalRelay) -> None:
        await wall(ctx, "standup now", "30m", clear=False)
        result = await wall(ctx, "", "", clear=False)
        assert not result.error
        assert "standup now" in result.text
        assert isinstance(result.json_data, dict)

    async def test_clear_wall(self, ctx: CliContext, relay: LocalRelay) -> None:
        await wall(ctx, "temp wall", "1h", clear=False)
        result = await wall(ctx, "", "", clear=True)
        assert not result.error
        assert result.text == "Wall cleared."
        assert result.json_data == {"status": "cleared"}

        # Verify it's gone
        assert await relay.get_wall() is None

    async def test_bad_duration(self, ctx: CliContext) -> None:
        result = await wall(ctx, "test", "xyz", clear=False)
        assert result.error
        assert "Unrecognized duration" in result.text
        data = cast("dict[str, object]", result.json_data)
        assert "error" in data

    async def test_sanitize_message(self, ctx: CliContext) -> None:
        result = await wall(ctx, "  hello\x00world  ", "1h", clear=False)
        assert not result.error
        assert "helloworld" in result.text

    async def test_empty_after_sanitize(self, ctx: CliContext) -> None:
        # Message with only whitespace/control chars becomes empty → read mode
        result = await wall(ctx, "   \x00\x01   ", "", clear=False)
        assert not result.error
        assert result.text == "No active wall."

    async def test_default_duration(self, ctx: CliContext) -> None:
        # Empty duration defaults to 1h
        result = await wall(ctx, "default ttl", "", clear=False)
        assert not result.error
        assert "Wall posted" in result.text

    async def test_message_truncated(self, ctx: CliContext) -> None:
        long_msg = "x" * 600
        result = await wall(ctx, long_msg, "1h", clear=False)
        assert not result.error
        data = cast("dict[str, object]", result.json_data)
        text = data["text"]
        assert isinstance(text, str)
        assert len(text) == 512

    async def test_duration_too_long(self, ctx: CliContext) -> None:
        result = await wall(ctx, "test", "10d", clear=False)
        assert result.error
        assert "exceeds maximum" in result.text

    async def test_expired_wall_returns_none(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        from datetime import UTC, datetime, timedelta

        from biff.models import WallPost

        # Manually set an expired wall
        now = datetime.now(UTC)
        expired = WallPost(
            text="old news",
            from_user="kai",
            from_tty="cli",
            posted_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),
        )
        await relay.set_wall(expired)

        result = await wall(ctx, "", "", clear=False)
        assert result.text == "No active wall."

    async def test_validation_error_on_empty_user(self, relay: LocalRelay) -> None:
        # Bypass BiffConfig validation to exercise the WallPost
        # ValidationError catch (from_user min_length=1).
        bad_config = BiffConfig.model_construct(user="", repo_name="test")
        bad_ctx = CliContext(
            relay=relay,
            config=bad_config,
            session_key=":abc12345",
            user="",
            tty="abc12345",
        )
        result = await wall(bad_ctx, "test", "1h", clear=False)
        assert result.error
