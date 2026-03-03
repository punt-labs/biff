"""Tests for ``biff.commands.tty``."""

from __future__ import annotations

from biff.cli_session import CliContext
from biff.commands.tty import tty
from biff.models import UserSession
from biff.relay import LocalRelay


class TestTty:
    async def test_explicit_name(self, ctx: CliContext, relay: LocalRelay) -> None:
        result = await tty(ctx, "dev")
        assert not result.error
        assert result.text == "TTY: dev"
        assert result.json_data == {"tty": "dev"}

        session = await relay.get_session("kai:abc12345")
        assert session is not None
        assert session.tty_name == "dev"

    async def test_auto_name(self, ctx: CliContext) -> None:
        result = await tty(ctx, "")
        assert not result.error
        assert result.text == "TTY: tty1"
        assert result.json_data == {"tty": "tty1"}

    async def test_auto_name_increments(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        await relay.update_session(
            UserSession(user="eric", tty="eee11111", tty_name="tty1")
        )
        result = await tty(ctx, "")
        assert result.text == "TTY: tty2"

    async def test_name_too_long(self, ctx: CliContext) -> None:
        result = await tty(ctx, "a" * 21)
        assert result.error
        assert "20 characters" in result.text

    async def test_name_collision(self, ctx: CliContext, relay: LocalRelay) -> None:
        # Another session owned by kai with name "dev"
        await relay.update_session(
            UserSession(user="kai", tty="other123", tty_name="dev")
        )
        result = await tty(ctx, "dev")
        assert result.error
        assert "already in use" in result.text

    async def test_same_name_on_different_user_ok(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        await relay.update_session(
            UserSession(user="eric", tty="eee11111", tty_name="dev")
        )
        result = await tty(ctx, "dev")
        assert not result.error
        assert result.text == "TTY: dev"

    async def test_name_exactly_20_chars(self, ctx: CliContext) -> None:
        name = "a" * 20
        result = await tty(ctx, name)
        assert not result.error
        assert result.json_data == {"tty": name}

    async def test_rename_existing_session(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        await relay.update_session(
            UserSession(user="kai", tty="abc12345", tty_name="old")
        )
        result = await tty(ctx, "new")
        assert not result.error
        assert result.text == "TTY: new"

        session = await relay.get_session("kai:abc12345")
        assert session is not None
        assert session.tty_name == "new"

    async def test_whitespace_only_name_gets_auto(self, ctx: CliContext) -> None:
        result = await tty(ctx, "   ")
        assert not result.error
        # Whitespace-only should be treated as empty → auto-naming
        assert "tty" in result.text
