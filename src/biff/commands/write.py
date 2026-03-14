"""``biff write`` — send a message to a teammate."""

from __future__ import annotations

from biff.cli_session import CliContext
from biff.commands._result import CommandResult
from biff.models import Message
from biff.server.tools._session import resolve_tty_name
from biff.tty import build_session_key, parse_address


async def write(ctx: CliContext, to: str, message: str) -> CommandResult:
    """Send a message to a teammate's inbox."""
    try:
        bare_user, tty = parse_address(to)
    except ValueError as exc:
        return CommandResult(
            text=str(exc),
            json_data={"status": "error", "to": to, "error": str(exc)},
            error=True,
        )
    target_repo: str | None = None
    if tty:
        # Search across visible repos for the target session.
        all_sessions = await ctx.relay.get_sessions_for_repos(ctx.config.visible_repos)
        session = resolve_tty_name(
            all_sessions, bare_user, tty, local_repo=ctx.config.repo_name
        )
        if session:
            relay_key = build_session_key(session.user, session.tty)
            if session.repo and session.repo != ctx.config.repo_name:
                target_repo = session.repo
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
    try:
        await ctx.relay.deliver(
            msg, sender_key=ctx.session_key, target_repo=target_repo
        )
    except ValueError as exc:
        return CommandResult(
            text=str(exc),
            json_data={"status": "error", "to": to, "error": str(exc)},
            error=True,
        )
    return CommandResult(
        text=f"Message sent to {display}.",
        json_data={"status": "sent", "to": display},
    )
