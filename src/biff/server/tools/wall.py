"""Team broadcast tool — ``wall``.

``/wall`` posts a time-limited banner visible to all teammates on their
status bar and tool descriptions.  Unlike ``/write``, wall messages do
not go into inboxes and do not require ``/read``.

BSD ``wall(1)`` wrote to every logged-in terminal.  Biff adds
duration-based persistence and explicit clearing.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import ValidationError

from biff.formatting import (
    format_remaining,
    format_wall,
    parse_duration,
    sanitize_wall_message,
)
from biff.models import WallPost
from biff.nats_relay import NatsRelay
from biff.relay import Relay
from biff.server.tools._activate import auto_enable
from biff.server.tools._descriptions import refresh_wall
from biff.server.tools._session import update_current_session
from biff.server.tools._tasks import fire_and_forget

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState

_log = logging.getLogger(__name__)


WALL_BASE_DESCRIPTION = (
    "Broadcast a message to the whole team. "
    "The message appears on every teammate's status bar and tool list. "
    "Use clear=True to remove an active wall."
)


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the wall tool."""

    @mcp.tool(name="wall", description=WALL_BASE_DESCRIPTION)
    @auto_enable(state)
    async def wall(
        message: str = "", duration: str = "", clear: bool = False, repo: str = ""
    ) -> str:
        """Post, read, or clear a team broadcast wall.

        Four modes:

        - ``wall(message="text")`` — post to all visible repos (default 1h TTL)
        - ``wall(message="text", repo="punt-labs__vox")`` — post to one repo
        - ``wall(clear=True)`` — remove the active wall from all visible repos
        - ``wall()`` — show the current wall
        """
        # Validate repo against visible_repos (authorization + injection guard)
        target_repo = _validate_target_repo(repo, state.config.visible_repos)
        if repo and target_repo is None:
            return f"Repo {repo!r} is not in your visible repos."

        session = await update_current_session(state)

        # Clear mode
        if clear:
            await refresh_wall(mcp, state, wall=None)
            fire_and_forget(
                _broadcast_wall(state, wall=None, target_repo=target_repo),
                logger=_log,
                description="wall clear",
            )
            _update_wall_marker(state, wall=None)
            return "Wall cleared."

        # Sanitize: strip control chars, collapse to single line
        message = sanitize_wall_message(message)
        if not message:
            current = await state.relay.get_wall()
            if current is None:
                await refresh_wall(mcp, state, wall=None)
                return "No active wall."
            return format_wall(current)

        # Post mode
        try:
            ttl = parse_duration(duration)
        except ValueError as exc:
            return str(exc)

        now = datetime.now(UTC)
        message = message[:512]
        try:
            post = WallPost(
                text=message,
                from_user=state.config.user,
                from_tty=session.tty_name or session.tty,
                posted_at=now,
                expires_at=now + ttl,
            )
        except ValidationError as exc:
            for err in exc.errors():
                if err.get("type") == "string_too_short":
                    return "Message cannot be blank."
            return str(exc)
        await refresh_wall(mcp, state, wall=post)
        fire_and_forget(
            _broadcast_wall(state, wall=post, target_repo=target_repo),
            logger=_log,
            description="wall post",
        )
        _update_wall_marker(state, wall=post)

        remaining = format_remaining(post.expires_at)
        return f"Wall posted ({remaining}): {message}"


def _validate_target_repo(repo: str, visible_repos: frozenset[str]) -> str | None:
    """Validate a repo name against visible_repos.

    Returns the repo name if valid, ``None`` if *repo* is empty (broadcast
    to all), or ``None`` if the repo is not in visible_repos (caller
    should check ``repo and result is None`` to distinguish).
    """
    if not repo:
        return None
    if repo not in visible_repos:
        return None
    return repo


async def _broadcast_wall(
    state: ServerState, *, wall: WallPost | None, target_repo: str | None
) -> None:
    """Write a wall to one or all visible repos.

    When *target_repo* is set, writes only to that repo.  Otherwise
    writes to all repos in ``visible_repos`` (DES-030).
    """
    await broadcast_wall_to_repos(
        state.relay, state.config.visible_repos, wall=wall, target_repo=target_repo
    )


async def broadcast_wall_to_repos(
    relay: Relay,
    visible_repos: frozenset[str],
    *,
    wall: WallPost | None,
    target_repo: str | None,
) -> None:
    """Shared wall broadcast logic for MCP tool and CLI paths.

    Uses ``return_exceptions=True`` so a failure on one repo does not
    cancel writes to other repos.  Partial failures are logged.
    """
    if target_repo is not None:
        if isinstance(relay, NatsRelay):
            await relay.set_wall_for_repo(target_repo, wall)
        else:
            await relay.set_wall(wall)
        return
    # Broadcast to all visible repos
    if isinstance(relay, NatsRelay):
        repos = [r for r in visible_repos if r]  # skip empty (LocalRelay compat)
        tasks = [relay.set_wall_for_repo(r, wall) for r in repos]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for repo, result in zip(repos, results, strict=True):
            if isinstance(result, BaseException):
                _log.warning("Wall broadcast failed for repo %s: %s", repo, result)
    else:
        await relay.set_wall(wall)


def _update_wall_marker(state: ServerState, wall: WallPost | None) -> None:
    """Write or clear the wall marker for SessionStart hooks."""
    from biff.markers import clear_wall_marker, write_wall_marker  # noqa: PLC0415

    worktree = str(state.repo_root) if state.repo_root else ""
    if wall is not None:
        write_wall_marker(worktree, wall.text, wall.expires_at)
    else:
        clear_wall_marker(worktree)
