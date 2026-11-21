"""Shared session helper for CLI command implementations."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from biff.cli_context import CliContext
from biff.models import UserSession
from biff.tty import get_hostname, get_pwd

logger = logging.getLogger(__name__)


async def update_current_session(ctx: CliContext, **updates: object) -> UserSession:
    """Update this CLI session and refresh both activity timestamps.

    Advance last_tool_at as well as last_active so commands in a long-lived
    REPL reset the idle timer. Rebuild a missing row from this process's
    context, preserving repository, identity, and claimed TTY together.

    Raises:
        OSError: Local relay storage cannot be read or written.
        ConnectionError: The network relay cannot reconnect.
        TimeoutError: A relay request exceeds its deadline.
    """
    session = await ctx.relay.get_session(ctx.session_key)
    if session is None:
        # cli_session() always registers the row before yielding a
        # CliContext to a command, so reaching this fallback means the
        # row was deleted out from under a still-running process (reaper
        # sentinel, TTL expiry, manual delete) -- for a long-lived REPL,
        # an operator's ``mesg off`` would silently flip back on, and set
        # plan text would silently be erased. Surface it rather than
        # rebuild the record without a trace.
        logger.warning(
            "Session missing at update time; rebuilding from process environment"
        )
        session = UserSession(
            user=ctx.user,
            tty=ctx.tty,
            tty_name=ctx.tty_name or "cli",
            hostname=get_hostname(),
            pwd=get_pwd(),
            repo=ctx.config.repo_name,
            kind=ctx.config.kind,
            launched_by=ctx.config.launched_by,
            owner_git=ctx.config.owner_git,
        )
    now = datetime.now(UTC)
    updates["last_active"] = now
    updates["last_tool_at"] = now
    updated = session.model_copy(update=updates)
    await ctx.relay.update_session(updated)
    return updated
