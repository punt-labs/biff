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

``NatsRelay`` (in :mod:`biff.nats_relay`) implements the same protocol
over a NATS server for networked deployments.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from biff.models import Message, UnreadSummary, UserSession, build_unread_summary

logger = logging.getLogger(__name__)


def atomic_write(path: Path, content: str) -> None:
    """Atomically write *content* to *path* using temp-file-then-replace.

    Creates parent directories if needed. On failure, the temp file
    is cleaned up and the original file is left untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(content)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class Relay(Protocol):
    """Interface between an MCP server and the message relay."""

    # -- Messages --

    async def deliver(self, message: Message) -> None: ...

    async def fetch(self, user: str) -> list[Message]: ...

    async def mark_read(self, user: str, ids: Sequence[uuid.UUID]) -> None: ...

    async def get_unread_summary(self, user: str) -> UnreadSummary: ...

    # -- Presence --

    async def update_session(self, session: UserSession) -> None: ...

    async def get_session(self, user: str) -> UserSession | None: ...

    async def heartbeat(self, user: str) -> None: ...

    async def get_sessions(self) -> list[UserSession]: ...

    # -- Lifecycle --

    async def close(self) -> None: ...


class LocalRelay:
    """Filesystem-backed relay with per-user inbox files.

    Each user gets their own inbox file (``inbox-{user}.jsonl``).
    Sessions are stored in a single shared ``sessions.json``.
    All writes use temp-file-then-replace for atomicity.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    @staticmethod
    def _validate_user(user: str) -> str:
        """Reject usernames that could escape the data directory."""
        if not user or "/" in user or "\\" in user or ".." in user:
            msg = f"Invalid username: {user!r}"
            raise ValueError(msg)
        return user

    def _inbox_path(self, user: str) -> Path:
        return self._data_dir / f"inbox-{self._validate_user(user)}.jsonl"

    # -- Messages --

    async def deliver(self, message: Message) -> None:
        """Deliver a message to the recipient's inbox."""
        self._validate_user(message.from_user)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        path = self._inbox_path(message.to_user)
        with path.open("a") as f:
            f.write(message.model_dump_json() + "\n")

    async def fetch(self, user: str) -> list[Message]:
        """Get unread messages for a user, oldest first."""
        return [m for m in self._read_inbox(user) if not m.read]

    async def mark_read(self, user: str, ids: Sequence[uuid.UUID]) -> None:
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

    async def get_unread_summary(self, user: str) -> UnreadSummary:
        """Build an unread summary for dynamic tool descriptions."""
        unread = await self.fetch(user)
        return build_unread_summary(unread, len(unread))

    # -- Presence --

    async def update_session(self, session: UserSession) -> None:
        """Create or update a user's session."""
        user = self._validate_user(session.user)
        sessions = self._read_sessions()
        sessions[user] = session
        self._write_sessions(sessions)

    async def get_session(self, user: str) -> UserSession | None:
        """Get a specific user's session."""
        user = self._validate_user(user)
        return self._read_sessions().get(user)

    async def heartbeat(self, user: str) -> None:
        """Update last_active timestamp, creating session if needed."""
        user = self._validate_user(user)
        sessions = self._read_sessions()
        existing = sessions.get(user)
        if existing:
            sessions[user] = existing.model_copy(
                update={"last_active": datetime.now(UTC)}
            )
        else:
            sessions[user] = UserSession(user=user)
        self._write_sessions(sessions)

    async def get_sessions(self) -> list[UserSession]:
        """Get all sessions."""
        return list(self._read_sessions().values())

    async def close(self) -> None:
        """No-op — filesystem relay has no connection to close."""

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
        content = "".join(msg.model_dump_json() + "\n" for msg in messages)
        atomic_write(self._inbox_path(user), content)

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
        data = {k: v.model_dump(mode="json") for k, v in sessions.items()}
        atomic_write(
            self._data_dir / "sessions.json",
            json.dumps(data, indent=2) + "\n",
        )
