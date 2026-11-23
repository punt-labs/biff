"""``biff status`` — show connection state and session info."""

from __future__ import annotations

from importlib.metadata import version as pkg_version
from typing import TYPE_CHECKING

from biff.commands._relay_line import RelayLine
from biff.commands._result import CommandResult
from biff.formatting import format_idle, format_wall_status_line

if TYPE_CHECKING:
    from biff.cli_context import CliContext


async def status(ctx: CliContext) -> CommandResult:
    """Show connection state, session info, and pending messages."""
    ver = pkg_version("punt-biff")

    session = await ctx.relay.get_session(ctx.session_key)
    total_unread = (await ctx.relay.get_unread_summary(ctx.session_key)).count
    wall_post = await ctx.relay.get_wall()

    relay = RelayLine.of(ctx.relay, ctx.config.relay_url)
    json_data: dict[str, object] = {
        "version": ver,
        "relay": relay.name,
        "relay_dir": relay.directory,
        "user": ctx.user,
        "session_key": ctx.session_key,
        "tty_name": session.tty_name if session else "cli",
        "unread": total_unread,
        "wall": wall_post.model_dump(mode="json") if wall_post else None,
    }

    tty_name = session.tty_name if session else "cli"
    idle = format_idle(session.last_active) if session else "?"
    lines = [
        f"biff {ver}",
        relay.text,
        f"user: {ctx.user}",
        f"session: {tty_name} ({ctx.tty[:8]}), idle {idle}",
        f"unread: {total_unread} message{'s' if total_unread != 1 else ''}",
    ]
    lines.append(format_wall_status_line(wall_post) if wall_post else "wall: (none)")

    return CommandResult(text="\n".join(lines), json_data=json_data)
