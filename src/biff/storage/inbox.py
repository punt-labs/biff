"""JSONL message storage.

Messages are stored as one JSON object per line in ``inbox.jsonl``.
Reads filter by recipient; writes are append-only for new messages
and atomic-rewrite for mark-read updates.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from biff.models import Message, UnreadSummary

logger = logging.getLogger(__name__)

_MAX_PREVIEW_LEN = 80
_MAX_BODY_PREVIEW = 40
_MAX_PREVIEW_MESSAGES = 3


class MessageStore:
    """JSONL-backed message store."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._inbox_path = data_dir / "inbox.jsonl"

    def append(self, message: Message) -> None:
        """Append a message to the inbox."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        with self._inbox_path.open("a") as f:
            f.write(message.model_dump_json() + "\n")

    def get_unread(self, user: str) -> list[Message]:
        """Get unread messages for a user, oldest first."""
        return [m for m in self._read_all() if m.to_user == user and not m.read]

    def mark_read(self, message_ids: Sequence[uuid.UUID]) -> None:
        """Mark messages as read by ID. Rewrites the file atomically."""
        ids = set(message_ids)
        messages = self._read_all()
        updated = [
            msg.model_copy(update={"read": True}) if msg.id in ids else msg
            for msg in messages
        ]
        self._write_all(updated)

    def get_unread_summary(self, user: str) -> UnreadSummary:
        """Build an unread summary for dynamic tool descriptions."""
        unread = self.get_unread(user)
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

    def _read_all(self) -> list[Message]:
        """Read all messages, skipping malformed lines."""
        if not self._inbox_path.exists():
            return []
        messages: list[Message] = []
        for line in self._inbox_path.read_text().splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                messages.append(Message.model_validate_json(stripped))
            except (ValidationError, ValueError):
                logger.warning("Skipping malformed inbox line: %s", stripped[:80])
        return messages

    def _write_all(self, messages: Sequence[Message]) -> None:
        """Atomically rewrite the inbox file."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._inbox_path.with_suffix(".tmp")
        with tmp.open("w") as f:
            for msg in messages:
                f.write(msg.model_dump_json() + "\n")
        tmp.rename(self._inbox_path)
