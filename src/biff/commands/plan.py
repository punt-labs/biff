"""``biff plan`` — set what you're currently working on."""

from __future__ import annotations

import os

from biff._stdlib import expand_bead_id, get_repo_common_root
from biff.cli_session import CliContext
from biff.commands._result import CommandResult
from biff.commands._session import update_current_session
from biff.formatting import format_talk_echo
from biff.markers import clear_plan_marker, write_plan_marker
from biff.session_routing import ID_RE


def cli_session_id() -> str | None:
    """The session Claude Code declares to a CLI process, or ``None``.

    Claude Code sets ``CLAUDE_CODE_SESSION_ID`` in the environment of each
    Bash tool call.  It is the harness stating the session, read only by the
    CLI, which has no ``_caller``; the server and hooks never read it.  A
    value outside the identity charset is treated as absent.
    """
    value = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    return value if ID_RE.fullmatch(value) else None


def sync_plan_marker(message: str, session_id: str | None = None) -> None:
    """Write or clear the plan-gate marker for *session_id*.

    The MCP tool passes the session the caller declared.  The CLI passes
    nothing and uses :func:`cli_session_id`, so its marker is scoped to the
    session that ran it: another session's edit gate never reads it, and that
    session's SessionStart clears it.  With no session at all (headless) the
    marker goes to the shared bucket, which only an identity-less payload
    reads.  The root is resolved fresh on every call so a long-running
    server and a one-shot CLI invocation agree with the hook.
    """
    root = get_repo_common_root()
    identity = session_id if session_id is not None else cli_session_id()
    if message:
        write_plan_marker(root, identity, message)
    else:
        clear_plan_marker(root, identity)


async def plan(ctx: CliContext, message: str) -> CommandResult:
    """Set what you're currently working on."""
    message = expand_bead_id(message)
    await update_current_session(ctx, plan=message, plan_source="manual")
    sync_plan_marker(message)
    return CommandResult(
        text=format_talk_echo("Plan:", message),
        json_data={"plan": message},
    )
