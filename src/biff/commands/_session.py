"""Shared session helper for CLI command implementations."""

from __future__ import annotations

from datetime import UTC, datetime

from biff.cli_session import CliContext
from biff.models import UserSession
from biff.tty import get_hostname, get_pwd


async def update_current_session(ctx: CliContext, **updates: object) -> UserSession:
    """Update this CLI session, refreshing both activity timestamps.

    Mirrors :func:`biff.server.tools._session.update_current_session` for
    the CLI surface: reaching this call means a real CLI command ran (a
    human or agent typed ``biff plan``/``tty``/``mesg``), so both
    ``last_active`` and ``last_tool_at`` advance.  ``plan``, ``tty``, and
    ``mesg`` each used to repeat this get-or-create-then-``model_copy``
    dance inline and only ever touched the fields they cared about --
    never ``last_tool_at``.  For a one-shot CLI invocation that is
    invisible (the session is deleted on exit), but a long-lived
    interactive REPL (``cli_session(interactive=True)``) never advances
    ``last_tool_at`` past registration, since only the background
    heartbeat loop touches ``last_active``.  An actively-used REPL then
    reads as increasingly idle the longer it runs (biff-liu round 2).

    A missing session is backfilled the same way each of the three
    call sites did on their own: a fresh :class:`UserSession` with
    ``tty_name`` from *ctx* (``"cli"`` when the context has none yet).
    """
    session = await ctx.relay.get_session(ctx.session_key)
    if session is None:
        session = UserSession(
            user=ctx.user,
            tty=ctx.tty,
            tty_name=ctx.tty_name or "cli",
            hostname=get_hostname(),
            pwd=get_pwd(),
        )
    now = datetime.now(UTC)
    updates["last_active"] = now
    updates["last_tool_at"] = now
    updated = session.model_copy(update=updates)
    await ctx.relay.update_session(updated)
    return updated
