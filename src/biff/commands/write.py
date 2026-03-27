"""``biff write`` — send a message to a teammate."""

from __future__ import annotations

from biff.chunking import chunk_message
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
        all_sessions = await ctx.relay.get_sessions_for_repos(ctx.visible_repos)
        session = resolve_tty_name(
            all_sessions, bare_user, tty, local_repo=ctx.config.repo_name
        )
        if session:
            relay_key = build_session_key(session.user, session.tty)
            if session.repo and session.repo != ctx.config.repo_name:
                target_repo = session.repo
        else:
            user_exists = any(s.user == bare_user for s in all_sessions)
            if user_exists:
                err = (
                    f"No active session @{bare_user}:{tty}."
                    f" Try @{bare_user} to broadcast."
                )
            else:
                err = f"User @{bare_user} not found in visible repos."
            return CommandResult(
                text=err,
                json_data={"status": "error", "to": to, "error": err},
                error=True,
            )
    else:
        relay_key = bare_user
    display = f"@{bare_user}:{tty}" if tty else f"@{bare_user}"

    chunks = chunk_message(message)
    try:
        for chunk in chunks:
            msg = Message(
                from_user=ctx.user,
                from_tty=ctx.tty_name,
                to_user=relay_key,
                body=chunk,
            )
            await ctx.relay.deliver(
                msg, sender_key=ctx.session_key, target_repo=target_repo
            )
    except Exception as exc:  # noqa: BLE001
        return CommandResult(
            text=str(exc),
            json_data={"status": "error", "to": to, "error": str(exc)},
            error=True,
        )
    parts = len(chunks)
    suffix = f" ({parts} parts)" if parts > 1 else ""
    return CommandResult(
        text=f"Message sent to {display}.{suffix}",
        json_data={"status": "sent", "to": display, "parts": parts},
    )
