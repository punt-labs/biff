"""``biff plan`` — set what you're currently working on."""

from __future__ import annotations

from biff._stdlib import expand_bead_id, get_repo_common_root
from biff.cli_session import CliContext
from biff.commands._result import CommandResult
from biff.formatting import format_talk_echo
from biff.markers import clear_plan_marker, write_plan_marker
from biff.models import UserSession
from biff.session_id import SessionHint
from biff.tty import get_hostname, get_pwd


def sync_plan_marker(message: str) -> None:
    """Write or clear the plan-gate marker for this process's session identity.

    Shared by the CLI ``plan`` command and the MCP ``plan`` tool
    (``server/tools/plan.py``) — before this, only the MCP tool wrote the
    marker, so a CLI-only session (every ethos-mission worker in this
    org) could run ``biff plan`` and see a success message while the
    ``PreToolUse`` gate kept denying edits: the marker it reads was never
    written.  Root and identity are
    resolved fresh on every call — never cached at process startup — so a
    long-running MCP server and a one-shot CLI invocation always resolve
    the same key the hook itself resolves.
    """
    root = get_repo_common_root()
    identity = SessionHint.resolve_routing_id()
    if message:
        write_plan_marker(root, identity, message)
    else:
        clear_plan_marker(root, identity)


async def plan(ctx: CliContext, message: str) -> CommandResult:
    """Set what you're currently working on."""
    message = expand_bead_id(message)
    session = await ctx.relay.get_session(ctx.session_key)
    if session is None:
        session = UserSession(
            user=ctx.user,
            tty=ctx.tty,
            tty_name="cli",
            hostname=get_hostname(),
            pwd=get_pwd(),
        )
    updated = session.model_copy(update={"plan": message, "plan_source": "manual"})
    await ctx.relay.update_session(updated)
    sync_plan_marker(message)
    return CommandResult(
        text=format_talk_echo("Plan:", message),
        json_data={"plan": message},
    )
