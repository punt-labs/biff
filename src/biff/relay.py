"""Relay protocol and local filesystem implementation.

The Relay abstracts how the MCP server communicates with the
message routing layer.  The MCP server is per-user; the relay
is shared.

Session keys are composite ``{user}:{tty}`` strings.  Each server
instance owns one session key.

``LocalRelay`` implements the relay over a shared filesystem
directory with per-session inbox files and a shared sessions file::

    {data_dir}/
        inbox-kai-a1b2c3d4.jsonl
        inbox-eric-12345678.jsonl
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
from biff.tty import build_session_key

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 259_200  # 3 days — covers weekends


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
    """Interface between an MCP server and the message relay.

    Session keys are ``{user}:{tty}`` composite strings.
    """

    # -- Messages --

    async def deliver(self, message: Message) -> None: ...

    async def fetch(self, session_key: str) -> list[Message]: ...

    async def mark_read(self, session_key: str, ids: Sequence[uuid.UUID]) -> None: ...

    async def get_unread_summary(self, session_key: str) -> UnreadSummary: ...

    # -- Presence --

    async def update_session(self, session: UserSession) -> None: ...

    async def get_session(self, session_key: str) -> UserSession | None: ...

    async def get_sessions_for_user(self, user: str) -> list[UserSession]: ...

    async def heartbeat(self, session_key: str) -> None: ...

    async def get_sessions(self) -> list[UserSession]: ...

    async def delete_session(self, session_key: str) -> None: ...

    # -- Lifecycle --

    async def close(self) -> None: ...


class LocalRelay:
    """Filesystem-backed relay with per-session inbox files.

    Each session gets its own inbox file (``inbox-{user}-{tty}.jsonl``).
    Sessions are stored in a single shared ``sessions.json`` keyed
    by ``{user}:{tty}``.  All writes use temp-file-then-replace for
    atomicity.
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

    def _validate_session_key(self, session_key: str) -> None:
        """Reject session keys that could escape the data directory."""
        if ":" not in session_key:
            msg = f"Invalid session key (missing ':'): {session_key!r}"
            raise ValueError(msg)
        user, tty = session_key.split(":", maxsplit=1)
        self._validate_user(user)
        if not tty or "/" in tty or "\\" in tty or ".." in tty or ":" in tty:
            msg = f"Invalid tty in session key: {tty!r}"
            raise ValueError(msg)

    def _inbox_path_for_key(self, session_key: str) -> Path:
        """Inbox file path for a session key (``{user}:{tty}``)."""
        self._validate_session_key(session_key)
        safe = session_key.replace(":", "-")
        return self._data_dir / f"inbox-{safe}.jsonl"

    # -- Messages --

    async def deliver(self, message: Message) -> None:
        """Deliver a message to the recipient's inbox.

        If ``to_user`` contains a ``:`` (targeted), deliver to one
        session inbox.  Otherwise (broadcast), deliver to all active
        sessions for that user.
        """
        self._validate_user(message.from_user)
        self._data_dir.mkdir(parents=True, exist_ok=True)

        if ":" in message.to_user:
            # Targeted delivery — single inbox
            user_part = message.to_user.split(":")[0]
            self._validate_user(user_part)
            path = self._inbox_path_for_key(message.to_user)
            with path.open("a") as f:
                f.write(message.model_dump_json() + "\n")
        else:
            # Broadcast — deliver to every session of this user
            self._validate_user(message.to_user)
            sessions = await self.get_sessions_for_user(message.to_user)
            if not sessions:
                # No active sessions — deliver to a bare-user inbox as fallback
                path = self._data_dir / f"inbox-{message.to_user}.jsonl"
                with path.open("a") as f:
                    f.write(message.model_dump_json() + "\n")
                return
            for s in sessions:
                key = build_session_key(s.user, s.tty)
                targeted = message.model_copy(update={"to_user": key})
                path = self._inbox_path_for_key(key)
                with path.open("a") as f:
                    f.write(targeted.model_dump_json() + "\n")

    async def fetch(self, session_key: str) -> list[Message]:
        """Get unread messages for a session, oldest first."""
        return [m for m in self._read_inbox(session_key) if not m.read]

    async def mark_read(self, session_key: str, ids: Sequence[uuid.UUID]) -> None:
        """Mark messages as read.  Rewrites the session's inbox atomically."""
        id_set = set(ids)
        if not id_set:
            return
        messages = self._read_inbox(session_key)
        updated: list[Message] = []
        changed = False
        for msg in messages:
            if msg.id in id_set and not msg.read:
                updated.append(msg.model_copy(update={"read": True}))
                changed = True
            else:
                updated.append(msg)
        if changed:
            self._write_inbox(session_key, updated)

    async def get_unread_summary(self, session_key: str) -> UnreadSummary:
        """Build an unread summary for dynamic tool descriptions."""
        unread = await self.fetch(session_key)
        return build_unread_summary(unread, len(unread))

    # -- Presence --

    async def update_session(self, session: UserSession) -> None:
        """Create or update a session (keyed by ``{user}:{tty}``)."""
        self._validate_user(session.user)
        key = build_session_key(session.user, session.tty)
        sessions = self._read_sessions()
        sessions[key] = session
        self._write_sessions(sessions)

    def _is_expired(self, session: UserSession) -> bool:
        """Check if a session has exceeded the idle TTL."""
        age = (datetime.now(UTC) - session.last_active).total_seconds()
        return age > SESSION_TTL_SECONDS

    async def get_session(self, session_key: str) -> UserSession | None:
        """Get a specific session by its ``{user}:{tty}`` key."""
        session = self._read_sessions().get(session_key)
        if session is not None and self._is_expired(session):
            return None
        return session

    async def get_sessions_for_user(self, user: str) -> list[UserSession]:
        """Get all sessions for a given user, reaping removals first."""
        self._validate_user(user)
        self.reap_sentinels()
        prefix = f"{user}:"
        return [
            s
            for k, s in self._read_sessions().items()
            if k.startswith(prefix) and not self._is_expired(s)
        ]

    async def heartbeat(self, session_key: str) -> None:
        """Update last_active timestamp, creating session if needed."""
        self._validate_session_key(session_key)
        sessions = self._read_sessions()
        existing = sessions.get(session_key)
        if existing:
            sessions[session_key] = existing.model_copy(
                update={"last_active": datetime.now(UTC)}
            )
        else:
            user, tty = session_key.split(":", maxsplit=1)
            sessions[session_key] = UserSession(user=user, tty=tty)
        self._write_sessions(sessions)

    async def get_sessions(self) -> list[UserSession]:
        """Get all sessions, reaping removals and filtering expired."""
        self.reap_sentinels()
        return [s for s in self._read_sessions().values() if not self._is_expired(s)]

    async def delete_session(self, session_key: str) -> None:
        """Remove a session from storage."""
        self.delete_session_sync(session_key)

    def delete_session_sync(self, session_key: str) -> None:
        """Remove a session from storage (sync, safe from signal handlers)."""
        self._validate_session_key(session_key)
        sessions = self._read_sessions()
        if session_key in sessions:
            del sessions[session_key]
            self._write_sessions(sessions)

    def write_remove_sentinel(self, session_key: str) -> None:
        """Create a sentinel file marking a session for removal.

        The sentinel is a plain file whose content is the session key.
        Any server that calls :meth:`reap_sentinels` (via
        :meth:`get_sessions` or :meth:`get_sessions_for_user`) will
        delete the corresponding session and clean up the file.

        Safe to call from signal handlers — sync I/O only, no
        read-modify-write on the shared sessions file.
        """
        self._validate_session_key(session_key)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        safe = session_key.replace(":", "-")
        sentinel = self._data_dir / f"remove-{safe}"
        sentinel.write_text(session_key)

    def reap_sentinels(self) -> None:
        """Process sentinel files, removing flagged sessions.

        Reads each ``remove-*`` file, deletes the named session from
        ``sessions.json``, and removes the sentinel.  Called
        automatically by :meth:`get_sessions` and
        :meth:`get_sessions_for_user` so callers always see clean data.
        """
        if not self._data_dir.exists():
            return
        sentinels = list(self._data_dir.glob("remove-*"))
        if not sentinels:
            return
        sessions = self._read_sessions()
        changed = False
        for sentinel in sentinels:
            try:
                session_key = sentinel.read_text().strip()
            except OSError:
                continue
            if session_key in sessions:
                del sessions[session_key]
                changed = True
            sentinel.unlink(missing_ok=True)
        if changed:
            self._write_sessions(sessions)

    async def close(self) -> None:
        """No-op — filesystem relay has no connection to close."""

    # -- Internal I/O --

    def _read_inbox(self, session_key: str) -> list[Message]:
        """Read all messages from a session's inbox."""
        path = self._inbox_path_for_key(session_key)
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

    def _write_inbox(self, session_key: str, messages: Sequence[Message]) -> None:
        """Atomically rewrite a session's inbox."""
        content = "".join(msg.model_dump_json() + "\n" for msg in messages)
        atomic_write(self._inbox_path_for_key(session_key), content)

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
