"""``biff finger`` — check what a user is working on."""

from __future__ import annotations

from biff.cli_session import CliContext
from biff.commands._result import CommandResult
from biff.formatting import format_finger, format_finger_multi
from biff.server.tools._session import resolve_session
from biff.tty import parse_address


async def finger(ctx: CliContext, user: str) -> CommandResult:
    """Check what a user is working on and their availability."""
    bare_user, tty = parse_address(user)
    if tty:
        session = await resolve_session(ctx.relay, bare_user, tty)
        if session is None:
            return CommandResult(
                text=f"Login: {bare_user}\nNo session on tty {tty}.",
                json_data={"error": f"No session on tty {tty}."},
                error=True,
            )
        return CommandResult(
            text=format_finger(session),
            json_data=session.model_dump(mode="json"),
        )
    sessions = await ctx.relay.get_sessions_for_user(bare_user)
    if not sessions:
        return CommandResult(
            text=f"Login: {bare_user}\nNever logged in.",
            json_data={"error": f"@{bare_user} never logged in."},
            error=True,
        )
    return CommandResult(
        text=format_finger_multi(sessions),
        json_data=[s.model_dump(mode="json") for s in sessions],
    )
