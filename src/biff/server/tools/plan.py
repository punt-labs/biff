"""Status set tool — ``/plan "msg"``.

Sets the current user's plan (what they're working on).
Auto-expands bead IDs to include the issue title (biff-5zq).
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import TYPE_CHECKING, Literal, cast

from biff.server.tools._activate import auto_enable
from biff.server.tools._descriptions import refresh_read_messages
from biff.server.tools._session import get_or_create_session, update_current_session

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState

_BEAD_ID_RE = re.compile(r"^[a-z]+-[a-z0-9]{2,4}$")


def expand_bead_id(message: str) -> str:
    """Expand a bare bead ID to ``<id>: <title>`` if possible.

    If the message matches the bead ID pattern and ``bd`` can resolve
    the title, returns the expanded form.  Otherwise returns the
    original message unchanged.
    """
    if not _BEAD_ID_RE.match(message):
        return message
    try:
        result = subprocess.run(  # noqa: S603
            ["bd", "show", message, "--json", "-q"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode != 0:
            return message
        data = json.loads(result.stdout)
        if isinstance(data, list) and data:
            items = cast("list[object]", data)
            first = items[0]
            if isinstance(first, dict):
                rec = cast("dict[str, object]", first)
                title = rec.get("title", "")
                if isinstance(title, str) and title:
                    return f"{message}: {title}"
    except (FileNotFoundError, json.JSONDecodeError, TimeoutError, OSError):
        pass
    return message


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the plan tool."""

    @mcp.tool(
        name="plan",
        description=(
            "Set what you're currently working on. "
            "Visible to teammates via /finger and /who."
        ),
    )
    @auto_enable(state)
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
        if source == "auto":
            session = await get_or_create_session(state)
            if session.plan_source == "manual" and session.plan:
                # Re-write the marker even though the relay plan is unchanged.
                # SessionStart clears the marker, so auto-plan calls after
                # a new session starts must restore it.
                from biff.markers import write_plan_marker  # noqa: PLC0415

                worktree = str(state.repo_root) if state.repo_root else ""
                write_plan_marker(worktree, session.plan)
                return f"Plan unchanged (manual): {session.plan}"
        message = expand_bead_id(message)
        await update_current_session(state, plan=message, plan_source=source)
        await refresh_read_messages(mcp, state)

        # Write/clear plan marker for PreToolUse gate (biff-vq5).
        from biff.markers import clear_plan_marker, write_plan_marker  # noqa: PLC0415

        worktree = str(state.repo_root) if state.repo_root else ""
        if message:
            write_plan_marker(worktree, message)
        else:
            clear_plan_marker(worktree)

        return f"Plan: {message}"
