"""``biff plan`` — set what you're currently working on."""

from __future__ import annotations

from pydantic import ValidationError

from biff.cli_context import CliContext
from biff.commands._result import CommandResult
from biff.commands._session import update_current_session
from biff.formatting import format_talk_echo
from biff.models import PLAN_MAX_LENGTH, validate_plan


async def plan(ctx: CliContext, message: str) -> CommandResult:
    """Set the session's plan to *message*.

    What only the calling process can do is the caller's: expanding a bead id
    and writing the plan-gate marker (:class:`biff.plan_marker.PlanMarker`).
    """
    try:
        validate_plan(message)
    except ValidationError:
        error = f"Plan too long: {len(message)} characters (max {PLAN_MAX_LENGTH})"
        return CommandResult(text=error, json_data={"error": error}, error=True)
    await update_current_session(ctx, plan=message, plan_source="manual")
    return CommandResult(
        text=format_talk_echo("Plan:", message),
        json_data={"plan": message},
    )
