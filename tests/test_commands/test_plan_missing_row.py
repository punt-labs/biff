"""A plan write that finds no presence row still validates and still guards."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from biff.commands.plan_source import PlanWrite
from biff.models import PLAN_MAX_LENGTH, UserSession
from biff.relay import LocalRelay

if TYPE_CHECKING:
    from pathlib import Path

    from biff.cli_context import CliContext

KEY = "kai:abc12345"


class RowAppearsOnRebuild(LocalRelay):
    """A relay where another writer recreates the row, with a manual plan, just
    as the plan write goes to rebuild it: the first ``get_session`` finds it."""

    appeared = False

    async def get_session(self, session_key: str) -> UserSession | None:
        if not self.appeared:
            self.appeared = True
            await self.update_session(
                UserSession(
                    user="kai", tty="abc12345", plan="mine", plan_source="manual"
                )
            )
        return await super().get_session(session_key)


async def test_an_over_long_plan_is_refused_when_the_row_is_missing(
    ctx: CliContext, relay: LocalRelay
) -> None:
    result = await PlanWrite(ctx, "x" * (PLAN_MAX_LENGTH + 1), "manual").run()
    assert result.error
    assert await relay.get_session(KEY) is None


async def test_an_over_long_auto_plan_is_refused_whatever_the_row(
    ctx: CliContext, relay: LocalRelay
) -> None:
    await relay.update_session(UserSession(user="kai", tty="abc12345"))
    result = await PlanWrite(ctx, "x" * (PLAN_MAX_LENGTH + 1), "auto").run()
    row = await relay.get_session(KEY)
    assert (result.error, row is not None and row.plan) == (True, "")


async def test_an_auto_plan_never_replaces_a_manual_plan_that_appears_on_rebuild(
    ctx: CliContext, tmp_path: Path
) -> None:
    racy = RowAppearsOnRebuild(tmp_path)
    result = await PlanWrite(replace(ctx, relay=racy), "hook plan", "auto").run()
    row = await racy.get_session(KEY)
    assert row is not None
    assert (row.plan, row.plan_source) == ("mine", "manual")
    assert result.text == "Plan unchanged (manual):\n   mine"


async def test_a_missing_row_is_rebuilt_with_the_plan(
    ctx: CliContext, relay: LocalRelay
) -> None:
    result = await PlanWrite(ctx, "fresh", "manual").run()
    row = await relay.get_session(KEY)
    assert row is not None
    assert (row.plan, row.plan_source, result.error) == ("fresh", "manual", False)
