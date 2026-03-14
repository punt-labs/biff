"""``biff finger`` — check what a user is working on."""

from __future__ import annotations

from biff.cli_session import CliContext
from biff.commands._result import CommandResult
from biff.formatting import format_finger, format_finger_multi
from biff.server.tools._session import resolve_tty_name
from biff.tty import parse_address


async def finger(ctx: CliContext, user: str) -> CommandResult:
    """Check what a user is working on and their availability."""
    try:
        bare_user, tty = parse_address(user)
    except ValueError as exc:
        return CommandResult(
            text=str(exc),
            json_data={"error": str(exc)},
            error=True,
        )
    all_sessions = await ctx.relay.get_sessions_for_repos(ctx.config.visible_repos)

    if tty:
        session = resolve_tty_name(
            all_sessions, bare_user, tty, local_repo=ctx.config.repo_name
        )
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
    sessions = [s for s in all_sessions if s.user == bare_user]
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
