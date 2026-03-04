"""Tests for ``biff.commands.finger``."""

from __future__ import annotations

from typing import cast

from biff.cli_session import CliContext
from biff.commands.finger import finger
from biff.models import UserSession
from biff.relay import LocalRelay


class TestFinger:
    async def test_user_never_logged_in(self, ctx: CliContext) -> None:
        result = await finger(ctx, "@nobody")
        assert result.error
        assert "Never logged in" in result.text

    async def test_user_with_session(self, ctx: CliContext, relay: LocalRelay) -> None:
        await relay.update_session(
            UserSession(
                user="eric", tty="def67890", tty_name="tty1", plan="reviewing PRs"
            )
        )
        result = await finger(ctx, "@eric")
        assert not result.error
        assert "eric" in result.text
        data = cast("list[dict[str, object]]", result.json_data)
        assert len(data) == 1

    async def test_specific_tty(self, ctx: CliContext, relay: LocalRelay) -> None:
        await relay.update_session(
            UserSession(user="eric", tty="def67890", tty_name="tty1")
        )
        result = await finger(ctx, "@eric:tty1")
        assert not result.error
        assert "eric" in result.text
        assert isinstance(result.json_data, dict)

    async def test_multiple_sessions(self, ctx: CliContext, relay: LocalRelay) -> None:
        await relay.update_session(
            UserSession(user="eric", tty="def67890", tty_name="tty1", plan="coding")
        )
        await relay.update_session(
            UserSession(user="eric", tty="ghi11111", tty_name="tty2", plan="reviewing")
        )
        result = await finger(ctx, "@eric")
        assert not result.error
        assert "eric" in result.text
        # format_finger_multi returns header + multiple tty blocks
        assert "coding" in result.text
        assert "reviewing" in result.text
        data = cast("list[dict[str, object]]", result.json_data)
        assert len(data) == 2

    async def test_specific_tty_not_found(self, ctx: CliContext) -> None:
        result = await finger(ctx, "@eric:nonexistent")
        assert result.error
        assert "No session on tty nonexistent" in result.text

    async def test_bare_user_without_at(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        await relay.update_session(
            UserSession(user="eric", tty="def67890", tty_name="tty1")
        )
        result = await finger(ctx, "eric")
        assert not result.error
        assert "eric" in result.text

    async def test_invalid_address_empty_tty(self, ctx: CliContext) -> None:
        result = await finger(ctx, "@eric:")
        assert result.error
        assert "Empty TTY" in result.text
