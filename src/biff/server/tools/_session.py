"""Shared session helpers for tool implementations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from biff.models import UserSession
    from biff.server.state import ServerState


def get_or_create_session(state: ServerState) -> UserSession:
    """Get the current user's session, creating one if it doesn't exist."""
    session = state.sessions.get_user(state.config.user)
    if session is None:
        state.sessions.heartbeat(state.config.user)
        session = state.sessions.get_user(state.config.user)
        assert session is not None  # noqa: S101
    return session


def update_current_session(state: ServerState, **updates: object) -> UserSession:
    """Update the current user's session with automatic last_active refresh."""
    session = get_or_create_session(state)
    updates["last_active"] = datetime.now(UTC)
    updated = session.model_copy(update=updates)
    state.sessions.update(updated)
    return updated
