"""JSON session and presence storage.

Sessions are stored as a JSON object keyed by username.
All writes are atomic (write to temp file, rename).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from biff.models import UserSession

logger = logging.getLogger(__name__)


class SessionStore:
    """JSON-backed session store."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._sessions_path = data_dir / "sessions.json"

    def update(self, session: UserSession) -> None:
        """Create or update a user's session."""
        sessions = self._read_all()
        sessions[session.user] = session
        self._write_all(sessions)

    def get_user(self, user: str) -> UserSession | None:
        """Get a specific user's session, or None if not found."""
        return self._read_all().get(user)

    def get_active(self, ttl: int = 120) -> list[UserSession]:
        """Get sessions active within the TTL (seconds)."""
        cutoff = datetime.now(UTC) - timedelta(seconds=ttl)
        return [s for s in self._read_all().values() if s.last_active >= cutoff]

    def heartbeat(self, user: str) -> None:
        """Update a user's last_active timestamp, creating if needed."""
        sessions = self._read_all()
        existing = sessions.get(user)
        if existing:
            sessions[user] = existing.model_copy(
                update={"last_active": datetime.now(UTC)}
            )
        else:
            sessions[user] = UserSession(user=user)
        self._write_all(sessions)

    def _read_all(self) -> dict[str, UserSession]:
        """Read all sessions from JSON."""
        if not self._sessions_path.exists():
            return {}
        try:
            data = json.loads(self._sessions_path.read_text())
            return {k: UserSession.model_validate(v) for k, v in data.items()}
        except (ValidationError, ValueError, json.JSONDecodeError, AttributeError):
            logger.warning("Corrupt sessions file, starting fresh")
            return {}

    def _write_all(self, sessions: dict[str, UserSession]) -> None:
        """Atomically rewrite the sessions file."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        data = {k: v.model_dump(mode="json") for k, v in sessions.items()}
        tmp = self._sessions_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2) + "\n")
            tmp.rename(self._sessions_path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
