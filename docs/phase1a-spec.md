# Phase 1A Technical Specification: Core Infrastructure

**Start Date**: TBD
**Duration**: 1 week
**Blocking**: Phase 1B (Message Tools)
**Dependencies**: None

## Overview

Phase 1A implements the foundational data models, storage layer, and local relay for same-machine communication. This phase has no user-facing features; it establishes the infrastructure for all subsequent phases.

## Module Structure

```
src/biff/
├── models.py           # Pydantic models (NEW)
├── storage/            # Storage layer (NEW)
│   ├── __init__.py
│   ├── base.py         # Base storage protocol
│   ├── inbox.py        # Message inbox (inbox.jsonl)
│   ├── notifications.py # Notification count (notifications.json)
│   └── sessions.py     # Talk session state (talk_session.json)
└── relay/              # Local relay (NEW)
    ├── __init__.py
    └── local.py        # File-based local relay

tests/
├── test_models.py      # Model validation tests
├── test_storage_inbox.py
├── test_storage_notifications.py
├── test_storage_sessions.py
└── test_relay_local.py
```

## Data Models (`models.py`)

### Message

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal


@dataclass(frozen=True)
class Message:
    """A biff message."""

    id: str  # UUID4
    from_user: str  # @username
    to_user: str  # @username or @hive
    body: str
    timestamp: datetime  # UTC
    read: bool = False
    type: Literal["mesg", "wall", "talk"] = "mesg"

    @classmethod
    def create(
        cls, from_user: str, to_user: str, body: str, type: Literal["mesg", "wall", "talk"] = "mesg"
    ) -> Message:
        """Create a new message with generated ID and current timestamp."""
        import uuid
        return cls(
            id=str(uuid.uuid4()),
            from_user=from_user,
            to_user=to_user,
            body=body,
            timestamp=datetime.now(UTC),
            read=False,
            type=type,
        )

    def to_dict(self) -> dict[str, object]:
        """Serialize to dict for JSON storage."""
        return {
            "id": self.id,
            "from_user": self.from_user,
            "to_user": self.to_user,
            "body": self.body,
            "timestamp": self.timestamp.isoformat(),
            "read": self.read,
            "type": self.type,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Message:
        """Deserialize from dict."""
        return cls(
            id=str(data["id"]),
            from_user=str(data["from_user"]),
            to_user=str(data["to_user"]),
            body=str(data["body"]),
            timestamp=datetime.fromisoformat(str(data["timestamp"])),
            read=bool(data.get("read", False)),
            type=str(data.get("type", "mesg")),  # type: ignore[arg-type]
        )
```

**Tests:**
- Create message with defaults
- Serialize to dict, deserialize, verify equality
- Timestamp is UTC
- UUID is valid UUID4
- Frozen (immutable)

### User

```python
@dataclass(frozen=True)
class User:
    """A biff user."""

    username: str  # @username
    plan: str = ""  # .plan file content
    status: Literal["active", "away", "offline"] = "offline"
    last_seen: datetime | None = None  # UTC

    def to_dict(self) -> dict[str, object]:
        """Serialize to dict."""
        return {
            "username": self.username,
            "plan": self.plan,
            "status": self.status,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> User:
        """Deserialize from dict."""
        last_seen = data.get("last_seen")
        return cls(
            username=str(data["username"]),
            plan=str(data.get("plan", "")),
            status=str(data.get("status", "offline")),  # type: ignore[arg-type]
            last_seen=datetime.fromisoformat(str(last_seen)) if last_seen else None,
        )
```

**Tests:**
- Serialize/deserialize with all fields
- Serialize/deserialize with optional fields None
- Frozen (immutable)

### TalkSession

```python
@dataclass(frozen=True)
class TalkSession:
    """An active talk session."""

    user: str  # @username
    started_at: datetime  # UTC
    active: bool = True

    def to_dict(self) -> dict[str, object]:
        """Serialize to dict."""
        return {
            "user": self.user,
            "started_at": self.started_at.isoformat(),
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> TalkSession:
        """Deserialize from dict."""
        return cls(
            user=str(data["user"]),
            started_at=datetime.fromisoformat(str(data["started_at"])),
            active=bool(data.get("active", True)),
        )

    def is_stale(self, max_age_seconds: int = 3600) -> bool:
        """Check if session is stale (older than max_age_seconds)."""
        now = datetime.now(UTC)
        age = (now - self.started_at).total_seconds()
        return age > max_age_seconds
```

**Tests:**
- Serialize/deserialize
- `is_stale()` returns True for old sessions
- `is_stale()` returns False for recent sessions
- Frozen (immutable)

**LOC Estimate**: ~200 LOC (including docstrings and type hints)

## Storage Layer

### Base Protocol (`storage/base.py`)

```python
from __future__ import annotations

from typing import Protocol


class Storage(Protocol):
    """Protocol for storage backends."""

    def read(self) -> object:
        """Read data from storage."""
        ...

    def write(self, data: object) -> None:
        """Write data to storage."""
        ...

    def delete(self) -> None:
        """Delete data from storage."""
        ...
```

**LOC**: ~20 LOC

### Inbox Storage (`storage/inbox.py`)

**File**: `~/.biff/inbox.jsonl`

**Format**: JSONL (one JSON object per line)

```python
from __future__ import annotations

import json
from pathlib import Path

from biff.models import Message


class InboxStorage:
    """Append-only message inbox backed by JSONL file."""

    def __init__(self, path: Path | None = None) -> None:
        """Initialize inbox storage.

        Args:
            path: Path to inbox file. Defaults to ~/.biff/inbox.jsonl
        """
        if path is None:
            path = Path.home() / ".biff" / "inbox.jsonl"
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, message: Message) -> None:
        """Append a message to the inbox.

        Uses atomic write (write to temp file, then rename).
        """
        line = json.dumps(message.to_dict())
        # Atomic append: write to temp, then rename
        temp_path = self.path.with_suffix(".tmp")
        with temp_path.open("a") as f:
            f.write(line + "\n")
        temp_path.replace(self.path)

    def read_all(self) -> list[Message]:
        """Read all messages from inbox."""
        if not self.path.exists():
            return []

        messages = []
        with self.path.open() as f:
            for line in f:
                if line.strip():
                    data = json.loads(line)
                    messages.append(Message.from_dict(data))
        return messages

    def read_unread(self) -> list[Message]:
        """Read only unread messages."""
        return [msg for msg in self.read_all() if not msg.read]

    def mark_read(self, message_id: str) -> None:
        """Mark a message as read.

        Rewrites entire inbox file (acceptable for Phase 1).
        """
        messages = self.read_all()
        updated = []
        for msg in messages:
            if msg.id == message_id:
                # Create new message with read=True (immutable)
                msg = Message(
                    id=msg.id,
                    from_user=msg.from_user,
                    to_user=msg.to_user,
                    body=msg.body,
                    timestamp=msg.timestamp,
                    read=True,
                    type=msg.type,
                )
            updated.append(msg)

        # Rewrite entire file
        self._write_all(updated)

    def delete(self, message_id: str) -> None:
        """Delete a message."""
        messages = [msg for msg in self.read_all() if msg.id != message_id]
        self._write_all(messages)

    def _write_all(self, messages: list[Message]) -> None:
        """Rewrite entire inbox (atomic)."""
        temp_path = self.path.with_suffix(".tmp")
        with temp_path.open("w") as f:
            for msg in messages:
                f.write(json.dumps(msg.to_dict()) + "\n")
        temp_path.replace(self.path)

    def clear(self) -> None:
        """Delete all messages."""
        self.path.unlink(missing_ok=True)
```

**Tests:**
- Append message, verify file contains line
- Read all messages, verify deserialization
- Read unread only, verify filtering
- Mark read, verify updated
- Delete message, verify removed
- Clear inbox, verify file deleted
- Atomic write behavior (no partial writes)
- Handle missing file gracefully

**LOC**: ~150 LOC

### Notification Storage (`storage/notifications.py`)

**File**: `~/.biff/notifications.json`

**Format**: JSON

```json
{
  "count": 3,
  "updated_at": "2026-02-13T12:34:56.789Z"
}
```

```python
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


class NotificationStorage:
    """Lightweight notification count storage."""

    def __init__(self, path: Path | None = None) -> None:
        """Initialize notification storage.

        Args:
            path: Path to notification file. Defaults to ~/.biff/notifications.json
        """
        if path is None:
            path = Path.home() / ".biff" / "notifications.json"
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read_count(self) -> int:
        """Read unread message count."""
        if not self.path.exists():
            return 0

        with self.path.open() as f:
            data = json.load(f)
        return int(data.get("count", 0))

    def write_count(self, count: int) -> None:
        """Write unread message count (atomic)."""
        data = {
            "count": count,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        # Atomic write
        temp_path = self.path.with_suffix(".tmp")
        with temp_path.open("w") as f:
            json.dump(data, f)
        temp_path.replace(self.path)

    def increment(self) -> None:
        """Increment count by 1."""
        current = self.read_count()
        self.write_count(current + 1)

    def decrement(self) -> None:
        """Decrement count by 1 (never goes below 0)."""
        current = self.read_count()
        self.write_count(max(0, current - 1))

    def clear(self) -> None:
        """Set count to 0."""
        self.write_count(0)
```

**Tests:**
- Write count, read back, verify
- Increment, verify count increased
- Decrement, verify count decreased
- Decrement at 0, verify stays 0
- Clear, verify count is 0
- Handle missing file gracefully
- Atomic write behavior

**LOC**: ~80 LOC

### Session Storage (`storage/sessions.py`)

**File**: `~/.biff/talk_session.json`

**Format**: JSON (single session at a time for Phase 1)

```python
from __future__ import annotations

import json
from pathlib import Path

from biff.models import TalkSession


class SessionStorage:
    """Talk session state storage."""

    def __init__(self, path: Path | None = None) -> None:
        """Initialize session storage.

        Args:
            path: Path to session file. Defaults to ~/.biff/talk_session.json
        """
        if path is None:
            path = Path.home() / ".biff" / "talk_session.json"
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> TalkSession | None:
        """Read active session, or None if no session."""
        if not self.path.exists():
            return None

        with self.path.open() as f:
            data = json.load(f)
        return TalkSession.from_dict(data)

    def write(self, session: TalkSession) -> None:
        """Write session (atomic)."""
        temp_path = self.path.with_suffix(".tmp")
        with temp_path.open("w") as f:
            json.dump(session.to_dict(), f)
        temp_path.replace(self.path)

    def delete(self) -> None:
        """Delete session file."""
        self.path.unlink(missing_ok=True)

    def has_active_session(self) -> bool:
        """Check if there is an active session."""
        session = self.read()
        return session is not None and session.active
```

**Tests:**
- Write session, read back, verify
- Delete session, verify file removed
- `has_active_session()` returns True when active
- `has_active_session()` returns False when no session
- Handle missing file gracefully
- Atomic write behavior

**LOC**: ~70 LOC

## Local Relay (`relay/local.py`)

**Purpose**: Same-machine message passing for Phase 1. Uses file-based relay.

**File**: `~/.biff/relay/{username}/inbox.jsonl`

Each user has their own inbox file. When sending a message, write to recipient's inbox.

```python
from __future__ import annotations

from pathlib import Path

from biff.models import Message
from biff.storage.inbox import InboxStorage


class LocalRelay:
    """Local relay for same-machine communication."""

    def __init__(self, relay_dir: Path | None = None) -> None:
        """Initialize local relay.

        Args:
            relay_dir: Path to relay directory. Defaults to ~/.biff/relay
        """
        if relay_dir is None:
            relay_dir = Path.home() / ".biff" / "relay"
        self.relay_dir = relay_dir
        self.relay_dir.mkdir(parents=True, exist_ok=True)

    def send(self, message: Message) -> None:
        """Send a message to a user's inbox.

        Args:
            message: Message to send. message.to_user determines the recipient.
        """
        # Write to recipient's inbox
        recipient = message.to_user.lstrip("@")
        inbox_path = self.relay_dir / recipient / "inbox.jsonl"
        inbox = InboxStorage(inbox_path)
        inbox.append(message)

    def receive(self, username: str) -> list[Message]:
        """Receive all messages for a user.

        Args:
            username: Username (with or without @)

        Returns:
            List of all messages in user's inbox.
        """
        username = username.lstrip("@")
        inbox_path = self.relay_dir / username / "inbox.jsonl"
        if not inbox_path.exists():
            return []

        inbox = InboxStorage(inbox_path)
        return inbox.read_all()

    def receive_unread(self, username: str) -> list[Message]:
        """Receive only unread messages for a user."""
        username = username.lstrip("@")
        inbox_path = self.relay_dir / username / "inbox.jsonl"
        if not inbox_path.exists():
            return []

        inbox = InboxStorage(inbox_path)
        return inbox.read_unread()

    def mark_read(self, username: str, message_id: str) -> None:
        """Mark a message as read in user's inbox."""
        username = username.lstrip("@")
        inbox_path = self.relay_dir / username / "inbox.jsonl"
        if inbox_path.exists():
            inbox = InboxStorage(inbox_path)
            inbox.mark_read(message_id)

    def list_users(self) -> list[str]:
        """List all users with relay inboxes."""
        users = []
        for user_dir in self.relay_dir.iterdir():
            if user_dir.is_dir():
                users.append(user_dir.name)
        return users
```

**Tests:**
- Send message, verify written to recipient inbox
- Receive messages, verify all returned
- Receive unread, verify filtering
- Mark read, verify updated
- List users, verify all returned
- Handle nonexistent user gracefully

**LOC**: ~100 LOC

## File Locations

All biff data lives under `~/.biff/`:

```
~/.biff/
├── inbox.jsonl             # Local user's inbox (for check_messages tool)
├── notifications.json      # Unread count (for hook)
├── talk_session.json       # Active talk session state
└── relay/                  # Local relay
    ├── alice/
    │   └── inbox.jsonl     # Alice's relay inbox
    ├── bob/
    │   └── inbox.jsonl     # Bob's relay inbox
    └── charlie/
        └── inbox.jsonl
```

**Rationale for dual inbox:**
- `~/.biff/inbox.jsonl`: Local user's inbox (what they see with `/mesg check`)
- `~/.biff/relay/{user}/inbox.jsonl`: Relay inbox (where remote messages arrive)

Phase 1: these are the same directory (local relay writes to local inbox).
Phase 2: relay inbox syncs from network relay to local inbox.

## Exit Criteria

- [ ] All models have 100% test coverage
- [ ] All storage modules have 100% test coverage
- [ ] Local relay has 100% test coverage
- [ ] Quality gates pass: `uv run ruff check .`, `uv run mypy src/ tests/`, `uv run pytest`
- [ ] No `# type: ignore` comments (all types are exact)
- [ ] All functions have docstrings
- [ ] All public APIs have example usage in docstrings

## Performance Targets

- Append message: < 10ms
- Read inbox (100 messages): < 20ms
- Read notification count: < 1ms
- Write notification count: < 5ms
- Read/write session: < 5ms

**If targets not met**: Profile with `cProfile`, optimize hot paths.

## Non-Goals (Out of Scope)

- User-facing tools (Phase 1B)
- Hooks (Phase 1B)
- Network relay (Phase 2)
- Authentication (Phase 2)
- Message encryption (Phase 2)
- Multi-session talk (Phase 2)

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| File corruption | Atomic writes (write to .tmp, then rename) |
| Concurrent access | File locking (add in Phase 1B if needed) |
| Disk full | Catch `OSError`, return clear error message |
| Invalid JSON | Validate on read, skip malformed lines |

## Questions

1. **File locking**: Do we need file locking for concurrent access? (Start without, add if tests reveal races)
2. **Message retention**: How many messages to keep in inbox? (Start with unlimited, add rotation in Phase 1B if needed)
3. **Error handling**: Should storage failures crash or return `None`/empty list? (Return empty, log error)

## Next Steps

After Phase 1A complete:

1. **Phase 1B**: Implement `/mesg` tools using storage layer
2. **Hook validation**: Test UserPromptSubmit hook with notification storage
3. **Integration tests**: End-to-end message send/receive via relay
