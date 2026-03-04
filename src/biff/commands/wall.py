"""``biff wall`` — post, read, or clear a team broadcast."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import ValidationError

from biff.cli_session import CliContext
from biff.commands._result import CommandResult
from biff.formatting import (
    format_remaining,
    format_wall,
    parse_duration,
    sanitize_wall_message,
)
from biff.models import WallPost


async def wall(
    ctx: CliContext, message: str, duration: str, *, clear: bool
) -> CommandResult:
    """Post, read, or clear a team broadcast."""
    if clear:
        await ctx.relay.set_wall(None)
        return CommandResult(text="Wall cleared.", json_data={"status": "cleared"})

    message = sanitize_wall_message(message)
    if not message:
        current = await ctx.relay.get_wall()
        if current is None:
            return CommandResult(
                text="No active wall.",
                json_data={"status": "inactive", "wall": None},
            )
        return CommandResult(
            text=format_wall(current),
            json_data=current.model_dump(mode="json"),
        )

    try:
        ttl = parse_duration(duration)
    except ValueError as exc:
        return CommandResult(text=str(exc), json_data={"error": str(exc)}, error=True)

    now = datetime.now(UTC)
    message = message[:512]
    try:
        post = WallPost(
            text=message,
            from_user=ctx.user,
            from_tty="cli",
            posted_at=now,
            expires_at=now + ttl,
        )
    except ValidationError as exc:
        return CommandResult(text=str(exc), json_data={"error": str(exc)}, error=True)

    await ctx.relay.set_wall(post)
    remaining = format_remaining(post.expires_at)
    return CommandResult(
        text=f"Wall posted ({remaining}): {message}",
        json_data=post.model_dump(mode="json"),
    )
