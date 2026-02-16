"""Data models for biff communication.

All models are immutable (frozen) pydantic models with full type annotations.
Serialization to/from JSON is handled by pydantic for JSONL storage.

All string fields are stripped of leading/trailing whitespace at parse time.
All datetime fields are normalized to UTC; naive datetimes are rejected.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> uuid.UUID:
    return uuid.uuid4()


def _ensure_utc(v: datetime) -> datetime:
    """Normalize a tz-aware datetime to UTC. Reject naive datetimes."""
    if v.tzinfo is None:
        msg = "Naive datetimes are not allowed; provide a timezone"
        raise ValueError(msg)
    if v.tzinfo is not UTC and not _is_utc(v.tzinfo):
        return v.astimezone(UTC)
    return v


def _is_utc(tz: tzinfo) -> bool:
    """Check if a tzinfo is effectively UTC."""
    return tz.utcoffset(None) == UTC.utcoffset(None)


class Message(BaseModel):
    """A single async message between two users.

    Stored in JSONL format in the user's inbox. Once created, messages
    are immutable — the ``read`` flag is tracked by the storage layer
    rather than by mutating the message in place.
    """

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    id: uuid.UUID = Field(default_factory=_new_id)
    from_user: str = Field(min_length=1)
    to_user: str = Field(min_length=1)
    body: str = Field(min_length=1)
    timestamp: datetime = Field(default_factory=_utc_now)
    read: bool = False

    @field_validator("timestamp", mode="after")
    @classmethod
    def _normalize_timestamp(cls, v: datetime) -> datetime:
        return _ensure_utc(v)


class UserSession(BaseModel):
    """A user's active session and presence information.

    Sessions track who is online, what they're working on (plan),
    and whether they're accepting messages (biff_enabled).
    Session liveness is determined by comparing ``last_active``
    against a TTL (default 120s).
    """

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    user: str = Field(min_length=1)
    display_name: str = ""
    plan: str = ""
    last_active: datetime = Field(default_factory=_utc_now)
    biff_enabled: bool = True

    @field_validator("last_active", mode="after")
    @classmethod
    def _normalize_last_active(cls, v: datetime) -> datetime:
        return _ensure_utc(v)


@dataclass(frozen=True)
class RelayAuth:
    """Authentication credentials for a remote NATS relay.

    At most one field may be set.  Mutual exclusivity is enforced
    at config-parse time in :func:`~biff.config._extract_biff_fields`.
    """

    token: str | None = None
    """Shared secret token."""

    nkeys_seed: str | None = None
    """Path to an NKey seed file (``.nk``)."""

    user_credentials: str | None = None
    """Path to a NATS credentials file (``.creds``)."""


class BiffConfig(BaseModel):
    """Validated configuration from a ``.biff`` file.

    The ``.biff`` file lives in a repo root and defines the team roster
    and relay URL. Parsing is handled by ``config.py``; this model
    holds the validated result.
    """

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    user: str = Field(min_length=1)
    display_name: str = ""
    relay_url: str | None = None
    relay_auth: RelayAuth | None = None
    team: tuple[str, ...] = ()


class UnreadSummary(BaseModel):
    """Summary of unread messages for dynamic tool descriptions.

    Used by the message watcher to generate descriptions like
    ``"Check messages (2 unread: @kai about auth, @eric about lunch)"``.
    """

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    count: int = Field(default=0, ge=0)
    preview: str = ""


_MAX_PREVIEW_LEN = 80
_MAX_BODY_PREVIEW = 40
_MAX_PREVIEW_MESSAGES = 3


def build_unread_summary(messages: Sequence[Message], count: int) -> UnreadSummary:
    """Build an :class:`UnreadSummary` from a list of messages.

    Shared by both ``LocalRelay`` and ``NatsRelay`` to avoid
    duplicating preview-formatting logic.
    """
    if count == 0:
        return UnreadSummary()
    previews = [
        f"@{m.from_user} about {m.body[:_MAX_BODY_PREVIEW]}"
        for m in messages[:_MAX_PREVIEW_MESSAGES]
    ]
    preview = ", ".join(previews)
    if len(preview) > _MAX_PREVIEW_LEN:
        preview = preview[: _MAX_PREVIEW_LEN - 3] + "..."
    return UnreadSummary(count=count, preview=preview)
