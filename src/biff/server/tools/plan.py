"""Status set tool — ``/plan "msg"``.

Sets the current user's plan (what they're working on).
Auto-expands bead IDs to include the issue title.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from biff._stdlib import expand_bead_id
from biff.formatting import format_talk_echo
from biff.models import PLAN_MAX_LENGTH, validate_plan
from biff.server.tools._activity import track_activity
from biff.server.tools._descriptions import refresh_read_messages
from biff.server.tools._session import get_or_create_session, update_current_session

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the plan tool."""

    @mcp.tool(
        name="plan",
        description=(
            "Set your BSD-style .plan status: a short, ONE-LINE status of "
            "what you're working on right now (the classic Unix finger/.plan "
            "file), visible to teammates via /finger and /who. This is NOT a "
            "task plan, todo list, or step-by-step breakdown — keep it to "
            "around 40 characters. Example: 'refactoring the auth layer' or "
            "'biff-if2: CLI identity fix'."
        ),
    )
    @track_activity(state)
    async def plan(
        message: str,
        source: Literal["manual", "auto"] = "manual",
    ) -> str:
        """Publish a short one-line status visible in who and finger.

        Use this for what you are working on, not a task list. Bare task identifiers
        are expanded to their titles when the local tracker CLI is available.

        Args:
            message: A short status; empty clears the current plan.
            source: Hooks use 'auto'; explicit user updates use 'manual'. Automatic
                updates cannot overwrite a nonempty manual plan.

        Returns:
            The published plan, a notice preserving a manual plan, or an error
            explaining the maximum length when the expanded message is too long.

        Raises:
            OSError: Local relay storage cannot be accessed.
            ConnectionError: The network relay cannot establish a connection.
            TimeoutError: A relay operation exceeds its deadline.
            nats.errors.Error: A NATS request or connection fails.
        """
        # Marker write/clear is a thin wrapper around the same shared
        # function the CLI's `plan` command calls — see
        # commands/plan.py's `sync_plan_marker` docstring.
        from biff.commands.plan import sync_plan_marker  # noqa: PLC0415

        if source == "auto":
            session = await get_or_create_session(state)
            if session.plan_source == "manual" and session.plan:
                # Re-write the marker even though the relay plan is unchanged.
                # SessionStart clears the marker, so auto-plan calls after
                # a new session starts must restore it.
                sync_plan_marker(session.plan, state.bound_session_id)
                return format_talk_echo("Plan unchanged (manual):", session.plan)
        message = expand_bead_id(message)
        try:
            validate_plan(message)
        except ValidationError:
            return f"Plan too long: {len(message)} characters (max {PLAN_MAX_LENGTH})"
        await update_current_session(state, plan=message, plan_source=source)
        await refresh_read_messages(mcp, state)
        sync_plan_marker(message, state.bound_session_id)
        return format_talk_echo("Plan:", message)
