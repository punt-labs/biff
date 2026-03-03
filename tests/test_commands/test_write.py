"""Tests for ``biff.commands.write``."""

from __future__ import annotations

from biff.cli_session import CliContext
from biff.commands.write import write
from biff.models import UserSession
from biff.relay import LocalRelay


class TestWrite:
    async def test_send_to_user(self, ctx: CliContext, relay: LocalRelay) -> None:
        result = await write(ctx, "@eric", "Hello!")
        assert not result.error
        assert "Message sent to @eric" in result.text
        assert result.json_data == {"status": "sent", "to": "@eric"}

        # Verify message was delivered
        msgs = await relay.fetch_user_inbox("eric")
        assert len(msgs) == 1
        assert msgs[0].body == "Hello!"
        assert msgs[0].from_user == "kai"

    async def test_message_truncated(self, ctx: CliContext, relay: LocalRelay) -> None:
        long_msg = "x" * 600
        result = await write(ctx, "@eric", long_msg)
        assert not result.error
        msgs = await relay.fetch_user_inbox("eric")
        assert len(msgs) == 1
        assert len(msgs[0].body) == 512

    async def test_send_to_tty(self, ctx: CliContext) -> None:
        result = await write(ctx, "@eric:tty1", "targeted msg")
        assert not result.error
        assert "@eric:tty1" in result.text

    async def test_resolve_session_by_tty_name(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        # Register eric with a named TTY
        await relay.update_session(
            UserSession(user="eric", tty="def67890", tty_name="dev")
        )
        result = await write(ctx, "@eric:dev", "resolved!")
        assert not result.error
        assert "@eric:dev" in result.text

    async def test_bare_user_without_at(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        result = await write(ctx, "eric", "no prefix")
        assert not result.error
        msgs = await relay.fetch_user_inbox("eric")
        assert len(msgs) == 1
        assert msgs[0].body == "no prefix"
