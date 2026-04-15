"""Shared session helpers for tool implementations."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from biff.models import UserSession
from biff.server.tools._descriptions import get_tty_name

if TYPE_CHECKING:
    from biff.server.state import ServerState


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
        session = UserSession(
            user=state.config.user,
            tty=state.tty,
            tty_name=get_tty_name(),
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


async def update_companion_session(
    state: ServerState, **updates: object
) -> UserSession | None:
    """Update the companion (root) session with automatic last_active refresh.

    Returns ``None`` when no companion session is configured.
    """
    companion = state.companion
    if companion is None:
        return None
    session = await state.relay.get_session(companion.session_key)
    if session is None:
        # register_session() should have written this row on lifespan
        # startup; reaching here means either a caller invoked us before
        # registration or the row was deleted out from under us.  Refuse
        # to write a half-formed row without tty_name — that was the
        # v1.8.0 biff-dzqc defect.
        if "tty_name" not in updates or not updates["tty_name"]:
            msg = (
                f"update_companion_session on missing row "
                f"{companion.session_key!r} without tty_name — refusing to "
                "write a row with empty tty_name"
            )
            raise RuntimeError(msg)
        session = UserSession(
            user=companion.user,
            tty=companion.tty,
            tty_name=str(updates["tty_name"]),
            hostname=state.hostname,
            pwd=state.pwd,
            display_name=companion.display_name,
            kind=companion.kind,
            repo=state.config.repo_name,
        )
    updates["last_active"] = datetime.now(UTC)
    updated = session.model_copy(update=updates)
    await state.relay.update_session(updated)
    return updated
