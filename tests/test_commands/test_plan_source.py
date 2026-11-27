"""``biff.commands.plan``: a hook's automatic plan never beats a manual one."""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.commands.plan import plan
from biff.commands.plan_source import set_plan

if TYPE_CHECKING:
    from biff.cli_context import CliContext
    from biff.relay import LocalRelay


async def test_auto_sets_a_plan_when_none_is_set(
    ctx: CliContext, relay: LocalRelay
) -> None:
    result = await set_plan(ctx, "auto plan", "auto")
    assert result.text == "Plan:\n   auto plan"
    session = await relay.get_session("kai:abc12345")
    assert session is not None
    assert (session.plan, session.plan_source) == ("auto plan", "auto")


async def test_auto_never_overwrites_a_manual_plan(
    ctx: CliContext, relay: LocalRelay
) -> None:
    await plan(ctx, "mine")
    result = await set_plan(ctx, "hook plan", "auto")
    assert result.text == "Plan unchanged (manual):\n   mine"
    assert result.json_data == {"plan": "mine", "unchanged": True}
    session = await relay.get_session("kai:abc12345")
    assert session is not None
    assert (session.plan, session.plan_source) == ("mine", "manual")


async def test_auto_replaces_an_auto_plan(ctx: CliContext, relay: LocalRelay) -> None:
    await set_plan(ctx, "first", "auto")
    await set_plan(ctx, "second", "auto")
    session = await relay.get_session("kai:abc12345")
    assert session is not None
    assert session.plan == "second"


async def test_manual_replaces_an_auto_plan(ctx: CliContext, relay: LocalRelay) -> None:
    await set_plan(ctx, "first", "auto")
    await plan(ctx, "second")
    session = await relay.get_session("kai:abc12345")
    assert session is not None
    assert (session.plan, session.plan_source) == ("second", "manual")


async def test_auto_over_an_emptied_manual_plan_sets_it(ctx: CliContext) -> None:
    await plan(ctx, "")
    result = await set_plan(ctx, "hook plan", "auto")
    assert result.text == "Plan:\n   hook plan"
