"""Set a session's plan, with what only the calling process can do."""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff import commands
from biff._stdlib import expand_bead_id
from biff.plan_marker import PlanMarker

if TYPE_CHECKING:
    from biff.cli_context import CliContext
    from biff.commands import CommandResult

__all__ = ["set_plan"]


async def set_plan(ctx: CliContext, message: str) -> CommandResult:
    """Set the plan to *message* (empty clears it) and sync the plan-gate marker.

    The edit-gate hook keys on the harness session, so it wins whenever present,
    even with a CLI record attached (``-s``, pointer, auto-create).
    """
    message = expand_bead_id(message)
    result = await commands.plan(ctx, message)
    if not result.error:
        PlanMarker.sync(message, PlanMarker.declared_session() or ctx.record_session_id)
    return result
