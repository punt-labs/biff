"""``biff plan`` — set what you're currently working on."""

from __future__ import annotations

from biff._stdlib import expand_bead_id, get_repo_common_root
from biff.cli_session import CliContext
from biff.commands._result import CommandResult
from biff.commands._session import update_current_session
from biff.formatting import format_talk_echo
from biff.markers import clear_plan_marker, write_plan_marker


def sync_plan_marker(message: str, session_id: str | None = None) -> None:
    """Write or clear the plan-gate marker for *session_id*.

    Shared by the CLI ``plan`` command and the MCP ``plan`` tool.  The MCP
    tool passes the session the caller declared; the CLI has no declared
    session and writes the shared bucket (``None``), which the gate reads
    as the last rung of its ladder.  The root is resolved fresh on every
    call so a long-running server and a one-shot CLI invocation agree with
    the hook.
    """
    root = get_repo_common_root()
    if message:
        write_plan_marker(root, session_id, message)
    else:
        clear_plan_marker(root, session_id)


async def plan(ctx: CliContext, message: str) -> CommandResult:
    """Set what you're currently working on."""
    message = expand_bead_id(message)
    await update_current_session(ctx, plan=message, plan_source="manual")
    sync_plan_marker(message)
    return CommandResult(
        text=format_talk_echo("Plan:", message),
        json_data={"plan": message},
    )
