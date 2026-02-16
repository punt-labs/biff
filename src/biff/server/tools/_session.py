"""Shared session helpers for tool implementations."""

from __future__ import annotations

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
        )
        await state.relay.update_session(session)
    elif not session.display_name and state.config.display_name:
        session = session.model_copy(update={"display_name": state.config.display_name})
        await state.relay.update_session(session)
    return session


async def update_current_session(state: ServerState, **updates: object) -> UserSession:
    """Update this server's session with automatic last_active refresh."""
    session = await get_or_create_session(state)
    updates["last_active"] = datetime.now(UTC)
    updated = session.model_copy(update=updates)
    await state.relay.update_session(updated)
    return updated
