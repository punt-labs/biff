"""``biff mesg`` — control message reception."""

from __future__ import annotations

from biff.cli_session import CliContext
from biff.commands._result import CommandResult
from biff.models import UserSession
from biff.tty import get_hostname, get_pwd


async def mesg(ctx: CliContext, *, enabled: bool) -> CommandResult:
    """Control message reception (on/off)."""
    session = await ctx.relay.get_session(ctx.session_key)
    if session is None:
        session = UserSession(
            user=ctx.user,
            tty=ctx.tty,
            tty_name="cli",
            hostname=get_hostname(),
            pwd=get_pwd(),
        )
    updated = session.model_copy(update={"biff_enabled": enabled})
    await ctx.relay.update_session(updated)
    label = "y" if enabled else "n"
    return CommandResult(
        text=f"is {label}",
        json_data={"mesg": label},
    )
