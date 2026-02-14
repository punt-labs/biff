"""Data models for biff communication.

All models are immutable (frozen) pydantic models with full type annotations.
Serialization to/from JSON is handled by pydantic for JSONL storage.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> uuid.UUID:
    return uuid.uuid4()


class Message(BaseModel):
    """A single async message between two users.

    Stored in JSONL format in the user's inbox. Once created, messages
    are immutable — the ``read`` flag is tracked by the storage layer
    rather than by mutating the message in place.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=_new_id)
    from_user: str = Field(min_length=1)
    to_user: str = Field(min_length=1)
    body: str = Field(min_length=1)
    timestamp: datetime = Field(default_factory=_utc_now)
    read: bool = False


class UserSession(BaseModel):
    """A user's active session and presence information.

    Sessions track who is online, what they're working on (plan),
    and whether they're accepting messages (biff_enabled).
    Session liveness is determined by comparing ``last_active``
    against a TTL (default 120s).
    """

    model_config = ConfigDict(frozen=True)

    user: str = Field(min_length=1)
    plan: str = ""
    last_active: datetime = Field(default_factory=_utc_now)
    biff_enabled: bool = True


class BiffConfig(BaseModel):
    """Validated configuration from a ``.biff`` file.

    The ``.biff`` file lives in a repo root and defines the team roster
    and relay URL. Parsing is handled by ``config.py``; this model
    holds the validated result.
    """

    model_config = ConfigDict(frozen=True)

    user: str = Field(min_length=1)
    relay_url: str | None = None
    team: tuple[str, ...] = ()


class UnreadSummary(BaseModel):
    """Summary of unread messages for dynamic tool descriptions.

    Used by the message watcher to generate descriptions like
    ``"Check messages (2 unread: @kai about auth, @eric about lunch)"``.
    """

    model_config = ConfigDict(frozen=True)

    count: int = Field(default=0, ge=0)
    preview: str = ""
