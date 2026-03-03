"""Tests for ``biff.commands.plan``."""

from __future__ import annotations

from biff.cli_session import CliContext
from biff.commands.plan import plan
from biff.relay import LocalRelay


class TestPlan:
    async def test_set_plan(self, ctx: CliContext, relay: LocalRelay) -> None:
        result = await plan(ctx, "working on tests")
        assert not result.error
        assert result.text == "Plan: working on tests"
        assert result.json_data == {"plan": "working on tests"}

        # Verify session was updated
        session = await relay.get_session("kai:abc12345")
        assert session is not None
        assert session.plan == "working on tests"
        assert session.plan_source == "manual"

    async def test_update_existing_plan(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        await plan(ctx, "first plan")
        result = await plan(ctx, "second plan")
        assert not result.error
        assert result.text == "Plan: second plan"

        session = await relay.get_session("kai:abc12345")
        assert session is not None
        assert session.plan == "second plan"

    async def test_empty_plan(self, ctx: CliContext, relay: LocalRelay) -> None:
        result = await plan(ctx, "")
        assert not result.error
        assert result.text == "Plan: "
        assert result.json_data == {"plan": ""}

        session = await relay.get_session("kai:abc12345")
        assert session is not None
        assert session.plan == ""

    async def test_creates_session_when_none_exists(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        # No session registered yet — plan should create one
        session_before = await relay.get_session("kai:abc12345")
        assert session_before is None

        result = await plan(ctx, "bootstrapped")
        assert not result.error

        session = await relay.get_session("kai:abc12345")
        assert session is not None
        assert session.plan == "bootstrapped"
        assert session.tty_name == "cli"
