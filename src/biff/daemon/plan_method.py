"""The daemon's ``plan`` method: set the session's plan as a person or a hook."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from biff.commands.plan_source import PLAN_SOURCES, PlanWrite
from biff.daemon.refusal import RefusalError
from biff.frame_reply import ErrorCode

if TYPE_CHECKING:
    from biff.cli_context import CliContext
    from biff.commands import CommandResult
    from biff.daemon.params import Params

__all__ = ["PlanMethod"]


@final
class PlanMethod:
    """``plan``: ``message`` and an optional ``source``, ``manual`` or ``auto``."""

    __slots__ = ()

    @staticmethod
    async def plan(ctx: CliContext, p: Params) -> CommandResult:
        """Set the plan; an absent ``source`` is ``manual``.

        Raises:
            RefusalError: ``source`` is neither ``manual`` nor ``auto``.
        """
        message = p.text("message")
        source = p.text("source", "manual")
        p.done()
        for known in PLAN_SOURCES:
            if source == known:
                return await PlanWrite(ctx, message, known).run()
        msg = f"plan: source must be one of {', '.join(PLAN_SOURCES)}"
        raise RefusalError.of(ErrorCode.INVALID_PARAMS, msg)
