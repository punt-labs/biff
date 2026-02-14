"""Relay protocol and local filesystem implementation.

The Relay abstracts how the MCP server communicates with the
message routing layer.  The MCP server is per-user; the relay
is shared.

``LocalRelay`` implements the relay over a shared filesystem
directory with per-user inbox files and a shared sessions file::

    {data_dir}/
        inbox-kai.jsonl
        inbox-eric.jsonl
        sessions.json

In Phase 2, ``NatsRelay`` will implement the same protocol over
the network.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from biff.models import Message, UnreadSummary, UserSession

logger = logging.getLogger(__name__)

_MAX_PREVIEW_LEN = 80
_MAX_BODY_PREVIEW = 40
_MAX_PREVIEW_MESSAGES = 3


@runtime_checkable
class Relay(Protocol):
    """Interface between an MCP server and the message relay."""

    # -- Messages --

    def deliver(self, message: Message) -> None: ...

    def fetch(self, user: str) -> list[Message]: ...

    def mark_read(self, user: str, ids: Sequence[uuid.UUID]) -> None: ...

    def get_unread_summary(self, user: str) -> UnreadSummary: ...

    # -- Presence --

    def update_session(self, session: UserSession) -> None: ...

    def get_session(self, user: str) -> UserSession | None: ...

    def heartbeat(self, user: str) -> None: ...

    def get_active_sessions(self, *, ttl: int = 120) -> list[UserSession]: ...


class LocalRelay:
    """Filesystem-backed relay with per-user inbox files.

    Each user gets their own inbox file (``inbox-{user}.jsonl``).
    Sessions are stored in a single shared ``sessions.json``.
    All writes use temp-file-then-replace for atomicity.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def _inbox_path(self, user: str) -> Path:
        return self._data_dir / f"inbox-{user}.jsonl"

    # -- Messages --

    def deliver(self, message: Message) -> None:
        """Deliver a message to the recipient's inbox."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        path = self._inbox_path(message.to_user)
        with path.open("a") as f:
            f.write(message.model_dump_json() + "\n")

    def fetch(self, user: str) -> list[Message]:
        """Get unread messages for a user, oldest first."""
        return [m for m in self._read_inbox(user) if not m.read]

    def mark_read(self, user: str, ids: Sequence[uuid.UUID]) -> None:
        """Mark messages as read.  Rewrites the user's inbox atomically."""
        id_set = set(ids)
        if not id_set:
            return
        messages = self._read_inbox(user)
        updated: list[Message] = []
        changed = False
        for msg in messages:
            if msg.id in id_set and not msg.read:
                updated.append(msg.model_copy(update={"read": True}))
                changed = True
            else:
                updated.append(msg)
        if changed:
            self._write_inbox(user, updated)

    def get_unread_summary(self, user: str) -> UnreadSummary:
        """Build an unread summary for dynamic tool descriptions."""
        unread = self.fetch(user)
        if not unread:
            return UnreadSummary()
        previews = [
            f"@{m.from_user} about {m.body[:_MAX_BODY_PREVIEW]}"
            for m in unread[:_MAX_PREVIEW_MESSAGES]
        ]
        preview = ", ".join(previews)
        if len(preview) > _MAX_PREVIEW_LEN:
            preview = preview[: _MAX_PREVIEW_LEN - 3] + "..."
        return UnreadSummary(count=len(unread), preview=preview)

    # -- Presence --

    def update_session(self, session: UserSession) -> None:
        """Create or update a user's session."""
        sessions = self._read_sessions()
        sessions[session.user] = session
        self._write_sessions(sessions)

    def get_session(self, user: str) -> UserSession | None:
        """Get a specific user's session."""
        return self._read_sessions().get(user)

    def heartbeat(self, user: str) -> None:
        """Update last_active timestamp, creating session if needed."""
        sessions = self._read_sessions()
        existing = sessions.get(user)
        if existing:
            sessions[user] = existing.model_copy(
                update={"last_active": datetime.now(UTC)}
            )
        else:
            sessions[user] = UserSession(user=user)
        self._write_sessions(sessions)

    def get_active_sessions(self, *, ttl: int = 120) -> list[UserSession]:
        """Get sessions active within the TTL (seconds)."""
        cutoff = datetime.now(UTC) - timedelta(seconds=ttl)
        return [s for s in self._read_sessions().values() if s.last_active >= cutoff]

    # -- Internal I/O --

    def _read_inbox(self, user: str) -> list[Message]:
        """Read all messages from a user's inbox."""
        path = self._inbox_path(user)
        if not path.exists():
            return []
        messages: list[Message] = []
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                messages.append(Message.model_validate_json(stripped))
            except (ValidationError, ValueError):
                logger.warning("Skipping malformed inbox line: %s", stripped[:80])
        return messages

    def _write_inbox(self, user: str, messages: Sequence[Message]) -> None:
        """Atomically rewrite a user's inbox."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        path = self._inbox_path(user)
        tmp = path.with_suffix(".tmp")
        try:
            with tmp.open("w") as f:
                for msg in messages:
                    f.write(msg.model_dump_json() + "\n")
            tmp.replace(path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _read_sessions(self) -> dict[str, UserSession]:
        """Read all sessions."""
        path = self._data_dir / "sessions.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text())
            return {k: UserSession.model_validate(v) for k, v in data.items()}
        except (ValidationError, ValueError, json.JSONDecodeError, AttributeError):
            logger.warning("Corrupt sessions file, starting fresh")
            return {}

    def _write_sessions(self, sessions: dict[str, UserSession]) -> None:
        """Atomically rewrite the sessions file."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        path = self._data_dir / "sessions.json"
        tmp = path.with_suffix(".tmp")
        try:
            data = {k: v.model_dump(mode="json") for k, v in sessions.items()}
            tmp.write_text(json.dumps(data, indent=2) + "\n")
            tmp.replace(path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
