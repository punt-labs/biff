"""Shared session helpers for tool implementations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from biff.models import UserSession
    from biff.server.state import ServerState


async def get_or_create_session(state: ServerState) -> UserSession:
    """Get the current user's session, creating one if it doesn't exist.

    Backfills ``display_name`` from config when the session has none,
    ensuring both new and pre-existing sessions pick up the value.
    """
    session = await state.relay.get_session(state.config.user)
    if session is None:
        await state.relay.heartbeat(state.config.user)
        session = await state.relay.get_session(state.config.user)
        assert session is not None  # noqa: S101
    if not session.display_name and state.config.display_name:
        session = session.model_copy(update={"display_name": state.config.display_name})
        await state.relay.update_session(session)
    return session


async def update_current_session(state: ServerState, **updates: object) -> UserSession:
    """Update the current user's session with automatic last_active refresh."""
    session = await get_or_create_session(state)
    updates["last_active"] = datetime.now(UTC)
    updated = session.model_copy(update=updates)
    await state.relay.update_session(updated)
    return updated
