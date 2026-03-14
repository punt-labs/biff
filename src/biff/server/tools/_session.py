"""Shared session helpers for tool implementations."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from biff.models import UserSession

if TYPE_CHECKING:
    from biff.server.state import ServerState


async def get_or_create_session(state: ServerState) -> UserSession:
    """Get this server's session, creating one if it doesn't exist.

    Uses ``state.session_key`` (``{user}:{tty}``) to look up the
    session, backfilling display_name, hostname, and pwd from the
    server state when creating a fresh session.
    """
    session = await state.relay.get_session(state.session_key)
    if session is None:
        session = UserSession(
            user=state.config.user,
            tty=state.tty,
            hostname=state.hostname,
            pwd=state.pwd,
            display_name=state.config.display_name,
            repo=state.config.repo_name,
        )
        await state.relay.update_session(session)
    else:
        # Backfill fields that may be missing from pre-DES-030 sessions
        # or from sessions created before display_name was resolved.
        updates: dict[str, object] = {}
        if not session.display_name and state.config.display_name:
            updates["display_name"] = state.config.display_name
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
