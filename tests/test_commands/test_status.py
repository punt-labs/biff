"""Tests for ``biff.commands.status``."""

from __future__ import annotations

from biff.cli_session import CliContext
from biff.commands.status import status
from biff.commands.wall import wall
from biff.models import UserSession
from biff.relay import LocalRelay


class TestStatus:
    async def test_basic_status(self, ctx: CliContext) -> None:
        result = await status(ctx)
        assert not result.error
        assert "biff" in result.text
        assert "kai" in result.text
        assert "relay: local (connected)" in result.text
        assert "unread: 0" in result.text
        assert "wall: (none)" in result.text
        json_data = result.json_data
        assert isinstance(json_data, dict)
        assert json_data["user"] == "kai"
        assert json_data["relay"] == "local"
        assert json_data["unread"] == 0
        assert json_data["wall"] is None

    async def test_status_with_session(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        await relay.update_session(
            UserSession(user="kai", tty="abc12345", tty_name="dev")
        )
        result = await status(ctx)
        assert not result.error
        assert "dev" in result.text
        json_data = result.json_data
        assert isinstance(json_data, dict)
        assert json_data["tty_name"] == "dev"

    async def test_status_with_unread(self, ctx: CliContext, relay: LocalRelay) -> None:
        from biff.models import Message

        # Targeted message to the TTY inbox
        await relay.deliver(
            Message(from_user="eric", to_user="kai:abc12345", body="hey"),
            sender_key="eric:def67890",
        )
        result = await status(ctx)
        assert not result.error
        assert "unread:" in result.text
        json_data = result.json_data
        assert isinstance(json_data, dict)
        assert json_data["unread"] >= 1

    async def test_status_with_active_wall(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        await wall(ctx, "deploy freeze", "1h", clear=False)
        result = await status(ctx)
        assert not result.error
        assert "deploy freeze" in result.text
        assert "wall:" in result.text
        assert "wall: (none)" not in result.text
        json_data = result.json_data
        assert isinstance(json_data, dict)
        assert json_data["wall"] is not None
