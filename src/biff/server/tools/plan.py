"""Status set tool — ``/plan "msg"``.

Sets the current user's plan (what they're working on).
Auto-expands bead IDs to include the issue title.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from biff._stdlib import expand_bead_id
from biff.formatting import format_talk_echo
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
        """Update the current user's ``.plan`` file.

        Bead IDs (e.g. ``biff-ka4``) are auto-expanded to include
        the issue title if ``bd`` is available::

            Plan: biff-ka4: post-checkout hook: update plan from branch

        The *source* parameter controls overwrite priority.
        Hooks pass ``"auto"``; manual ``/plan`` calls use the
        default ``"manual"``.  Auto plans cannot overwrite manual
        plans — the user's intentional plan takes precedence.
        """
        # Marker write/clear is a thin wrapper around the same shared
        # function the CLI's `plan` command calls (PL-PA-3) — see
        # commands/plan.py's `sync_plan_marker` docstring.
        from biff.commands.plan import sync_plan_marker  # noqa: PLC0415

        if source == "auto":
            session = await get_or_create_session(state)
            if session.plan_source == "manual" and session.plan:
                # Re-write the marker even though the relay plan is unchanged.
                # SessionStart clears the marker, so auto-plan calls after
                # a new session starts must restore it.
                sync_plan_marker(session.plan)
                return format_talk_echo("Plan unchanged (manual):", session.plan)
        message = expand_bead_id(message)
        await update_current_session(state, plan=message, plan_source=source)
        await refresh_read_messages(mcp, state)
        sync_plan_marker(message)
        return format_talk_echo("Plan:", message)
