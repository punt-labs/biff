"""Shared session helpers for tool implementations."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from biff.models import UserSession
from biff.server.tools._descriptions import get_tty_name

if TYPE_CHECKING:
    from biff.server.state import ServerState

logger = logging.getLogger(__name__)


async def get_or_create_session(state: ServerState) -> UserSession:
    """Get this server's session, creating one if it doesn't exist.

    Uses ``state.session_key`` (``{user}:{tty}``) to look up the
    session, backfilling display_name, hostname, pwd, and tty_name
    from the server state when creating a fresh session.

    The auto-create branch exists for test scaffolding and edge cases
    where a tool runs before ``register_session`` has completed.  It
    backfills ``tty_name`` from the session-local stash (set by the
    lifespan after claim) so a created-on-tool-call row is never
    written with an empty ``tty_name`` — guarding against the v1.8.0
    biff-dzqc defect resurfacing from a different code path.
    """
    session = await state.relay.get_session(state.session_key)
    if session is None:
        # Lifespan registration writes this row before any tool call.
        # Reaching the auto-create branch means the row was deleted
        # underneath us (reaper sentinel, TTL expiry, manual delete).
        # ``get_tty_name()`` may not be set yet if lifespan startup was
        # interrupted; fall back to the hex-slice placeholder that
        # ``_format_who_name`` uses so display stays consistent and the
        # v1.8.0 biff-dzqc invariant (no empty ``tty_name`` rows) holds.
        stashed = get_tty_name()
        if not stashed:
            logger.warning(
                "Auto-creating session %s without a reserved tty_name; "
                "lifespan registration may not have completed",
                state.session_key,
            )
        tty_name = stashed or state.tty[:8]
        session = UserSession(
            user=state.config.user,
            tty=state.tty,
            tty_name=tty_name,
            hostname=state.hostname,
            pwd=state.pwd,
            display_name=state.config.display_name,
            kind=state.config.kind,
            repo=state.config.repo_name,
        )
        await state.relay.update_session(session)
    else:
        # Backfill fields that may be missing from pre-DES-030 sessions
        # or from sessions created before display_name was resolved.
        updates: dict[str, object] = {}
        if not session.display_name and state.config.display_name:
            updates["display_name"] = state.config.display_name
        if not session.kind and state.config.kind:
            updates["kind"] = state.config.kind
        if not session.repo:
            updates["repo"] = state.config.repo_name
        if updates:
            session = session.model_copy(update=updates)
            await state.relay.update_session(session)
    return session


def resolve_tty_name(
    sessions: Sequence[UserSession],
    user: str,
    tty: str,
    *,
    local_repo: str = "",
) -> UserSession | None:
    """Resolve a tty identifier to a session from a pre-fetched list.

    Tries exact hex tty match first, then tty_name match.  When
    multiple sessions share the same tty_name across repos, prefers
    sessions in *local_repo* to avoid nondeterministic resolution.

    Returns ``None`` if no match.
    """
    # Exact hex tty match.
    session = next((s for s in sessions if s.user == user and s.tty == tty), None)
    if session is not None:
        return session
    # tty_name match — prefer local repo on ambiguity.
    candidates = [s for s in sessions if s.user == user and s.tty_name == tty]
    if not candidates:
        return None
    if local_repo:
        local = [s for s in candidates if s.repo == local_repo]
        if local:
            return local[0]
    return candidates[0]


async def update_current_session(state: ServerState, **updates: object) -> UserSession:
    """Update this server's session with automatic last_active refresh."""
    session = await get_or_create_session(state)
    updates["last_active"] = datetime.now(UTC)
    updated = session.model_copy(update=updates)
    await state.relay.update_session(updated)
    return updated
