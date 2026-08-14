"""Shared session helper for CLI command implementations."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from biff.cli_session import CliContext
from biff.models import UserSession
from biff.tty import get_hostname, get_pwd

logger = logging.getLogger(__name__)


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

    A missing session is backfilled from *ctx* -- the same identity and
    process environment every caller already has in hand.  Before this
    (biff-hvi), each of the three call sites built this fallback
    independently and two bugs crept in: ``repo`` was omitted entirely
    (defaulting to ``""``), and ``plan``/``mesg`` hardcoded
    ``tty_name="cli"`` instead of the ttyN this process actually
    claimed.  An empty ``repo`` isn't just cosmetic -- it drops the
    record out of ``get_sessions_for_repos`` filtering and out of
    ``resolve_tty_name``'s local-repo tiebreak -- and a later reader
    backfilling ``repo`` from ITS OWN config (e.g. an MCP server sharing
    the session key across repos, DES-034) can leave ``repo`` and
    ``pwd`` sourced from two different processes, so ``/who``'s REPO
    column and ``/finger``'s Dir disagree about the same session.
    Threading ``repo``, ``kind``, and the claimed ``tty_name`` from
    *ctx* here builds the same shaped record ``cli_session()``'s own
    registration does (``repo=config.repo_name``, ``kind=config.kind``)
    rather than a subset of it -- every field comes from the one
    process that is actually writing the record.
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
            "Session %s missing at update time; rebuilding from process environment",
            ctx.session_key,
        )
        session = UserSession(
            user=ctx.user,
            tty=ctx.tty,
            tty_name=ctx.tty_name or "cli",
            hostname=get_hostname(),
            pwd=get_pwd(),
            repo=ctx.config.repo_name,
            kind=ctx.config.kind,
        )
    now = datetime.now(UTC)
    updates["last_active"] = now
    updates["last_tool_at"] = now
    updated = session.model_copy(update=updates)
    await ctx.relay.update_session(updated)
    return updated
