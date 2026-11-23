"""``biff wall`` — post, read, or clear a team broadcast."""

from __future__ import annotations

import os
from datetime import UTC, datetime

from biff.cli_context import CliContext
from biff.commands._result import CommandResult
from biff.formatting import (
    format_wall,
    format_wall_confirmation,
    parse_duration,
    sanitize_wall_message,
)
from biff.models import WallPost
from biff.server.tools.wall import broadcast_wall_to_repos


async def _show(ctx: CliContext) -> CommandResult:
    """Return the active wall, or say there is none."""
    current = await ctx.relay.get_wall()
    if current is None:
        return CommandResult(
            text="No active wall.", json_data={"status": "inactive", "wall": None}
        )
    return CommandResult(
        text=format_wall(current), json_data=current.model_dump(mode="json")
    )


async def wall(
    ctx: CliContext, message: str, duration: str, *, clear: bool
) -> CommandResult:
    """Post, read, or clear a team broadcast."""
    if clear:
        await broadcast_wall_to_repos(
            ctx.relay, ctx.visible_repos, wall=None, target_repo=None
        )
        return CommandResult(text="Wall cleared.", json_data={"status": "cleared"})

    message = sanitize_wall_message(message)
    if not message:
        return await _show(ctx)

    try:
        if os.environ.get("GITHUB_ACTIONS") and not ctx.config.relay_url:
            # A CI runner is another machine: the local relay reaches no one.
            raise ValueError(
                "CI has no relay to post to: set the repository variable "
                "BIFF_RELAY_URL (vars.BIFF_RELAY_URL; a relay committed in "
                "config.yaml also needs relay.allow_env_override: true)"
            )
        post = WallPost(
            text=message[:512],
            from_user=ctx.user,
            from_tty="cli",
            posted_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + parse_duration(duration),
        )
    except ValueError as exc:  # includes pydantic's ValidationError
        return CommandResult(text=str(exc), json_data={"error": str(exc)}, error=True)

    await broadcast_wall_to_repos(
        ctx.relay, ctx.visible_repos, wall=post, target_repo=None
    )
    return CommandResult(
        text=format_wall_confirmation(post),
        json_data=post.model_dump(mode="json"),
    )
