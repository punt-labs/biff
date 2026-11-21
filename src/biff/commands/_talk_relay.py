"""Address resolution and safe failure metadata for ephemeral talk delivery."""

from __future__ import annotations

from traceback import walk_tb
from typing import TYPE_CHECKING

from biff.server.tools._session import resolve_talk_target

if TYPE_CHECKING:
    from biff.commands.talk import TalkContext


async def resolve_target(
    ctx: TalkContext, user: str, tty: str | None
) -> tuple[str, str]:
    """Resolve an address to ``(relay_key, display)``; raise if the user is offline.

    Talk is session-scoped (DES-043): the address must name a live session.
    ``resolve_talk_target`` maps a ``user:tty`` (or friendly ``tty_name``) to the
    peer's identity key, refusing a bare user or the caller's own session.

    Raises:
        ValueError: The target is offline, ambiguous, invalid, or the caller.
        OSError: Relay presence cannot be read.
        ConnectionError: The network relay cannot establish a connection.
        TimeoutError: The presence query exceeds its deadline.
        nats.errors.Error: The NATS presence query fails.
    """
    all_sessions = await ctx.relay.get_sessions_for_repos(ctx.visible_repos)
    if not any(s.user == user for s in all_sessions):
        msg = f"{user} is not online."
        raise ValueError(msg)
    return resolve_talk_target(
        all_sessions,
        user,
        tty,
        sender_key=ctx.session_key,
        sender_repo=ctx.config.repo_name,
    )


def publish_failure(exc: Exception) -> str:
    """Describe the failed stage and innermost source, never exception content."""
    step = location = "unknown"
    for frame, line in walk_tb(exc.__traceback__):
        module = frame.f_globals.get("__name__", "unknown")
        function = frame.f_code.co_name
        if (module, function) == ("biff.talk_state", "_publish"):
            # _publish binds nc only after connection acquisition succeeds.
            step = "publish" if "nc" in frame.f_locals else "connection"
        location = f"{module}/{function}:{line}"
    return f"step={step} error={type(exc).__name__} source={location}"
