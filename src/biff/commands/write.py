"""``biff write`` — send a message to a teammate."""

from __future__ import annotations

from biff.cli_session import CliContext
from biff.commands._result import CommandResult
from biff.models import Message
from biff.server.tools._session import resolve_session
from biff.tty import build_session_key, parse_address


async def write(ctx: CliContext, to: str, message: str) -> CommandResult:
    """Send a message to a teammate's inbox."""
    bare_user, tty = parse_address(to)
    if tty:
        session = await resolve_session(ctx.relay, bare_user, tty)
        if session:
            relay_key = build_session_key(session.user, session.tty)
        else:
            relay_key = f"{bare_user}:{tty}"
    else:
        relay_key = bare_user
    display = f"@{bare_user}:{tty}" if tty else f"@{bare_user}"

    msg = Message(
        from_user=ctx.user,
        to_user=relay_key,
        body=message[:512],
    )
    await ctx.relay.deliver(msg, sender_key=ctx.session_key)
    return CommandResult(
        text=f"Message sent to {display}.",
        json_data={"status": "sent", "to": display},
    )
