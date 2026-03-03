"""``biff status`` — show connection state and session info."""

from __future__ import annotations

from importlib.metadata import version as pkg_version

from biff.cli_session import CliContext
from biff.commands._result import CommandResult
from biff.formatting import format_idle, format_remaining


async def status(ctx: CliContext) -> CommandResult:
    """Show connection state, session info, and pending messages."""
    ver = pkg_version("punt-biff")

    session = await ctx.relay.get_session(ctx.session_key)
    summary = await ctx.relay.get_unread_summary(ctx.session_key)
    user_unread = await ctx.relay.get_user_unread_count(ctx.user)
    total_unread = summary.count + user_unread
    wall_post = await ctx.relay.get_wall()

    json_data: dict[str, object] = {
        "version": ver,
        "relay": ctx.config.relay_url,
        "user": ctx.user,
        "session_key": ctx.session_key,
        "tty_name": session.tty_name if session else "cli",
        "unread": total_unread,
        "wall": wall_post.model_dump(mode="json") if wall_post else None,
    }

    tty_name = session.tty_name if session else "cli"
    idle = format_idle(session.last_active) if session else "?"
    plural = "s" if total_unread != 1 else ""
    lines = [
        f"biff {ver}",
        f"relay: {ctx.config.relay_url} (connected)",
        f"user: {ctx.user}",
        f"session: {tty_name} ({ctx.tty[:8]}), idle {idle}",
        f"unread: {total_unread} message{plural}",
    ]
    if wall_post:
        remaining = format_remaining(wall_post.expires_at)
        lines.append(f"wall: @{wall_post.from_user}: {wall_post.text} ({remaining})")
    else:
        lines.append("wall: (none)")

    return CommandResult(text="\n".join(lines), json_data=json_data)
