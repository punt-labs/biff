"""Set a session's plan on behalf of a person or of a hook."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from biff.commands._result import CommandResult
from biff.commands._session import update_current_session
from biff.formatting import format_talk_echo
from biff.models import PLAN_MAX_LENGTH, validate_plan

if TYPE_CHECKING:
    from biff.cli_context import CliContext

__all__ = ["PLAN_SOURCES", "PlanSource", "set_plan"]

type PlanSource = Literal["manual", "auto"]
PLAN_SOURCES: tuple[PlanSource, ...] = ("manual", "auto")


async def set_plan(ctx: CliContext, message: str, source: PlanSource) -> CommandResult:
    """Set the session's plan to *message*, as *source* would.

    A hook sets ``auto``: its plan never replaces a non-empty manual one, and
    the reply says so (``unchanged``), so the caller still rewrites the
    plan-gate marker that a new session clears.

    What only the calling process can do is the caller's: expanding a bead id
    and writing that marker (:class:`biff.plan_marker.PlanMarker`).
    """
    if source == "auto":
        session = await ctx.relay.get_session(ctx.session_key)
        if session is not None and session.plan_source == "manual" and session.plan:
            return CommandResult(
                text=format_talk_echo("Plan unchanged (manual):", session.plan),
                json_data={"plan": session.plan, "unchanged": True},
            )
    try:
        validate_plan(message)
    except ValidationError:
        error = f"Plan too long: {len(message)} characters (max {PLAN_MAX_LENGTH})"
        return CommandResult(text=error, json_data={"error": error}, error=True)
    await update_current_session(ctx, plan=message, plan_source=source)
    return CommandResult(
        text=format_talk_echo("Plan:", message),
        json_data={"plan": message},
    )
