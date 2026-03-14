"""Data models for biff communication.

All models are immutable (frozen) pydantic models with full type annotations.
Serialization to/from JSON is handled by pydantic for JSONL storage.

All string fields are stripped of leading/trailing whitespace at parse time.
All datetime fields are normalized to UTC; naive datetimes are rejected.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Literal

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
    from_tty: str = Field(default="", description="Sender's human-readable tty name")
    to_user: str = Field(min_length=1)
    body: str = Field(min_length=1, max_length=512)
    timestamp: datetime = Field(default_factory=_utc_now)
    read: bool = False
    # Encryption envelope — reserved for biff-lff (DES-016).
    # All defaults are empty/false; populated when E2E encryption is active.
    encrypted: bool = False
    nonce: str = ""
    sender_pubkey: str = ""
    encryption_mode: str = ""

    @field_validator("timestamp", mode="after")
    @classmethod
    def _normalize_timestamp(cls, v: datetime) -> datetime:
        return _ensure_utc(v)


class UserSession(BaseModel):
    """A user's active session and presence information.

    Each server instance creates one session, identified by the
    composite key ``{user}:{tty}``.  The *tty* is a random 8-char
    hex string generated at startup.

    Sessions track who is online, what they're working on (plan),
    where they are (hostname, pwd), and whether they're accepting
    messages (biff_enabled).  Session liveness is determined by
    comparing ``last_active`` against a TTL (default 120s).
    """

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    user: str = Field(min_length=1)
    tty: str = Field(default="", description="8-char hex session identifier")
    tty_name: str = Field(default="", description="Human-readable session name")
    hostname: str = ""
    pwd: str = ""
    display_name: str = ""
    plan: str = ""
    plan_source: Literal["manual", "auto"] = "manual"
    last_active: datetime = Field(default_factory=_utc_now)
    biff_enabled: bool = True
    public_key: str = Field(
        default="",
        description="Base64-encoded Curve25519 public key; empty = no encryption",
    )
    repo: str = Field(
        default="",
        description="Repo name where this session is running; empty for LocalRelay",
    )

    @field_validator("last_active", mode="after")
    @classmethod
    def _normalize_last_active(cls, v: datetime) -> datetime:
        return _ensure_utc(v)


@dataclass(frozen=True)
class RelayAuth:
    """Authentication credentials for a remote NATS relay.

    At most one field may be set.  Mutual exclusivity is enforced
    at config-parse time in :func:`~biff.config.extract_biff_fields`.
    """

    token: str | None = None
    """Shared secret token."""

    nkeys_seed: str | None = None
    """Path to an NKey seed file (``.nk``)."""

    user_credentials: str | None = None
    """Path to a NATS credentials file (``.creds``)."""

    def as_nats_kwargs(self) -> dict[str, str]:
        """Build keyword arguments for ``nats.connect()``."""
        if self.token:
            return {"token": self.token}
        if self.nkeys_seed:
            return {"nkeys_seed": self.nkeys_seed}
        if self.user_credentials:
            return {"user_credentials": self.user_credentials}
        return {}


class BiffConfig(BaseModel):
    """Validated configuration from a ``.biff`` file.

    The ``.biff`` file lives in a repo root and defines the team roster
    and relay URL. Parsing is handled by ``config.py``; this model
    holds the validated result.
    """

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    user: str = Field(min_length=1)
    display_name: str = ""
    repo_name: str = Field(min_length=1)
    relay_url: str | None = None
    relay_auth: RelayAuth | None = None
    team: tuple[str, ...] = ()
    peers: tuple[str, ...] = ()

    @property
    def visible_repos(self) -> frozenset[str]:
        """Repos visible to this instance: self + peers."""
        return frozenset({self.repo_name, *self.peers})


class SessionEvent(BaseModel):
    """A login or logout event for the wtmp session ledger.

    Mirrors Unix ``wtmp`` records.  Login events are appended when a
    server starts; logout events are appended when a KV watcher
    observes a session deletion.

    ``version`` enables forward-compatible schema evolution for the
    durable wtmp stream.  Readers route on version to the appropriate
    validator; unrecognised versions are skipped rather than crashing.
    """

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    version: int = 1
    session_key: str = Field(min_length=1)
    event: str = Field(pattern=r"^(login|logout)$")
    user: str = Field(min_length=1)
    tty: str = ""
    tty_name: str = ""
    hostname: str = ""
    pwd: str = ""
    timestamp: datetime = Field(default_factory=_utc_now)
    plan: str = ""
    repo: str = ""

    @field_validator("timestamp", mode="after")
    @classmethod
    def _normalize_timestamp(cls, v: datetime) -> datetime:
        return _ensure_utc(v)


class WallPost(BaseModel):
    """A team broadcast banner with time-based expiry.

    Unlike messages (which go into inboxes and require ``/read``),
    a wall is immediately visible on the status bar and tool
    descriptions — fire-and-forget with automatic cleanup.
    """

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=512)
    from_user: str = Field(min_length=1)
    from_tty: str = Field(default="", description="Sender's human-readable tty name")
    posted_at: datetime = Field(default_factory=_utc_now)
    expires_at: datetime

    @field_validator("posted_at", "expires_at", mode="after")
    @classmethod
    def _normalize(cls, v: datetime) -> datetime:
        return _ensure_utc(v)

    @property
    def is_expired(self) -> bool:
        """Whether the wall has passed its expiry time."""
        return datetime.now(UTC) >= self.expires_at


class UnreadSummary(BaseModel):
    """Summary of unread messages for dynamic tool descriptions.

    Count-only (DES-015): no message preview.  The count drives the
    ``read_messages`` tool description (``"2 unread"``) and the
    ``tools/list_changed`` notification.  Eliminating the preview
    removed the last consumer-creating operation from the polling path.
    """

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    count: int = Field(default=0, ge=0)
