"""``biff tty`` — name the current CLI session."""

from __future__ import annotations

from biff.cli_session import CliContext
from biff.commands._result import CommandResult
from biff.models import UserSession
from biff.server.tools.tty import next_tty_name
from biff.tty import get_hostname, get_pwd


async def tty(ctx: CliContext, name: str) -> CommandResult:
    """Name the current CLI session."""
    name = name.strip()
    sessions = await ctx.relay.get_sessions()

    if not name:
        existing = [s.tty_name for s in sessions if s.tty_name]
        name = next_tty_name(existing)

    if len(name) > 20:
        msg = "Error: name must be 20 characters or fewer."
        return CommandResult(text=msg, json_data={"error": msg}, error=True)

    for s in sessions:
        if s.user == ctx.user and s.tty != ctx.tty and s.tty_name == name:
            msg = f"Error: name {name!r} already in use by another session."
            return CommandResult(text=msg, json_data={"error": msg}, error=True)

    session = await ctx.relay.get_session(ctx.session_key)
    if session is None:
        session = UserSession(
            user=ctx.user,
            tty=ctx.tty,
            tty_name=name,
            hostname=get_hostname(),
            pwd=get_pwd(),
        )
    else:
        session = session.model_copy(update={"tty_name": name})
    await ctx.relay.update_session(session)
    return CommandResult(
        text=f"TTY: {name}",
        json_data={"tty": name},
    )
