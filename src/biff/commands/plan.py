"""``biff plan`` — set what you're currently working on."""

from __future__ import annotations

from biff._stdlib import expand_bead_id
from biff.cli_session import CliContext
from biff.commands._result import CommandResult
from biff.models import UserSession
from biff.tty import get_hostname, get_pwd


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
    return CommandResult(
        text=f"Plan: {message}",
        json_data={"plan": message},
    )
