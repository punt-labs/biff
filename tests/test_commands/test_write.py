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
        assert result.json_data == {"status": "sent", "to": "@eric", "parts": 1}

        # Verify message was delivered
        msgs = await relay.fetch_user_inbox("eric")
        assert len(msgs) == 1
        assert msgs[0].body == "Hello!"
        assert msgs[0].from_user == "kai"

    async def test_long_message_chunked(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        long_msg = " ".join(["word"] * 200)  # ~1000 chars
        result = await write(ctx, "@eric", long_msg)
        assert not result.error
        msgs = await relay.fetch_user_inbox("eric")
        assert len(msgs) == 2
        assert all(len(m.body) <= 512 for m in msgs)
        reconstructed = " ".join(m.body for m in msgs)
        assert reconstructed == long_msg
        assert "(2 parts)" in result.text
        json_data = result.json_data
        assert isinstance(json_data, dict)
        assert json_data["parts"] == 2

    async def test_short_message_no_parts_label(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        result = await write(ctx, "@eric", "short")
        assert not result.error
        assert "parts" not in result.text
        msgs = await relay.fetch_user_inbox("eric")
        assert len(msgs) == 1
        assert msgs[0].body == "short"

    async def test_send_to_tty(self, ctx: CliContext, relay: LocalRelay) -> None:
        await relay.update_session(
            UserSession(user="eric", tty="abc12345", tty_name="tty1")
        )
        result = await write(ctx, "@eric:tty1", "targeted msg")
        assert not result.error
        assert "@eric:tty1" in result.text

    async def test_targeted_nonexistent_tty_error(self, ctx: CliContext) -> None:
        result = await write(ctx, "@eric:tty99", "hello")
        assert result.error
        assert "not found" in result.text

    async def test_targeted_wrong_tty_suggests_broadcast(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        await relay.update_session(
            UserSession(user="eric", tty="abc12345", tty_name="tty1")
        )
        result = await write(ctx, "@eric:tty99", "hello")
        assert result.error
        assert "Try @eric to broadcast" in result.text

    async def test_targeted_unknown_user_error(self, ctx: CliContext) -> None:
        result = await write(ctx, "@nobody:tty1", "hello")
        assert result.error
        assert "not found in visible repos" in result.text

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

    async def test_invalid_address_empty_tty(self, ctx: CliContext) -> None:
        result = await write(ctx, "@eric:", "hello")
        assert result.error
        assert "Empty TTY" in result.text
        assert result.json_data == {
            "status": "error",
            "to": "@eric:",
            "error": result.text,
        }
