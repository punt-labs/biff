"""Set a session's plan, with what only the calling process can do."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from biff import commands
from biff._stdlib import expand_bead_id
from biff.plan_marker import PlanMarker

if TYPE_CHECKING:
    from biff.cli_context import CliContext
    from biff.commands import CommandResult

__all__ = ["CallerPlan"]


@final
class CallerPlan:
    """The plan command as the process that ran it performs it."""

    @staticmethod
    async def set(ctx: CliContext, message: str) -> CommandResult:
        """Set the plan to *message* (empty clears it) and sync the plan-gate marker.

        The edit-gate hook keys on the harness session, so it wins whenever
        present, even with a CLI record attached (``-s``, pointer, auto-create).
        """
        message = expand_bead_id(message)
        result = await commands.plan(ctx, message)
        if not result.error:
            session = PlanMarker.declared_session() or ctx.record_session_id
            PlanMarker.sync(message, session)
        return result
