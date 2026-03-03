"""Tests for ``biff.commands.mesg``."""

from __future__ import annotations

from biff.cli_session import CliContext
from biff.commands.mesg import mesg
from biff.relay import LocalRelay


class TestMesg:
    async def test_disable(self, ctx: CliContext, relay: LocalRelay) -> None:
        result = await mesg(ctx, enabled=False)
        assert not result.error
        assert result.text == "is n"
        assert result.json_data == {"mesg": "n"}

        session = await relay.get_session("kai:abc12345")
        assert session is not None
        assert session.biff_enabled is False

    async def test_enable(self, ctx: CliContext, relay: LocalRelay) -> None:
        # First disable
        await mesg(ctx, enabled=False)
        # Then enable
        result = await mesg(ctx, enabled=True)
        assert not result.error
        assert result.text == "is y"
        assert result.json_data == {"mesg": "y"}

        session = await relay.get_session("kai:abc12345")
        assert session is not None
        assert session.biff_enabled is True

    async def test_idempotent_disable(self, ctx: CliContext, relay: LocalRelay) -> None:
        await mesg(ctx, enabled=False)
        result = await mesg(ctx, enabled=False)
        assert not result.error
        assert result.json_data == {"mesg": "n"}

        session = await relay.get_session("kai:abc12345")
        assert session is not None
        assert session.biff_enabled is False

    async def test_creates_session_when_none_exists(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        session_before = await relay.get_session("kai:abc12345")
        assert session_before is None

        result = await mesg(ctx, enabled=False)
        assert not result.error

        session = await relay.get_session("kai:abc12345")
        assert session is not None
        assert session.biff_enabled is False
