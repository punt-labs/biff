"""Data models for biff communication.

All models are immutable (frozen) pydantic models with full type annotations.
Serialization to/from JSON is handled by pydantic for JSONL storage.

All string fields are stripped of leading/trailing whitespace at parse time.
All datetime fields are normalized to UTC; naive datetimes are rejected.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
    # Encryption envelope, reserved for a future end-to-end encryption
    # feature (DES-016).  All defaults are empty/false; populated when
    # E2E encryption is active.
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
    messages (biff_enabled).  Liveness is checked via :meth:`is_live`,
    which compares ``last_active`` against a caller-supplied window
    (``PRESENCE_LIVENESS_SECONDS`` for presence surfaces) — the policy
    lives with the caller, not the model.

    Two timestamps track two different questions.  ``last_active`` is
    refreshed by the background heartbeat on a fixed interval regardless
    of activity — it answers "is the process alive" and is what
    :meth:`is_live` reads.  ``last_tool_at`` is refreshed only by a real
    tool invocation (``update_current_session``) — it answers
    "when did someone last actually do something", and is what the
    presence surfaces (``/who`` IDLE, ``/finger`` idle) display.
    Conflating the two made every live session's displayed idle time
    read as 0-1 minutes no matter how long it had actually sat unused,
    since the heartbeat kept the single shared field perpetually fresh.
    """

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    user: str = Field(min_length=1)
    tty: str = Field(default="", description="8-char hex session identifier")
    tty_name: str = Field(default="", description="Human-readable session name")
    hostname: str = ""
    pwd: str = ""
    display_name: str = ""
    kind: str = Field(
        default="",
        description="Identity kind: human, agent, or empty",
    )
    plan: str = ""
    plan_source: Literal["manual", "auto"] = "manual"
    last_active: datetime = Field(default_factory=_utc_now)
    last_tool_at: datetime = Field(
        default_factory=_utc_now,
        description=(
            "Timestamp of the last real tool invocation. "
            "Never None: a session with no invocation yet reads as its "
            "own start time, set at registration. Distinct from "
            "last_active, which the heartbeat refreshes every tick."
        ),
    )
    biff_enabled: bool = True
    public_key: str = Field(
        default="",
        description="Base64-encoded Curve25519 public key; empty = no encryption",
    )
    repo: str = Field(
        default="",
        description="Repo name where this session is running; empty for LocalRelay",
    )

    @model_validator(mode="before")
    @classmethod
    def _backfill_last_tool_at(cls, data: object) -> object:
        """Fall back ``last_tool_at`` to ``last_active`` for records written
        before this field existed.

        A KV/JSONL row written by a server that predates this field carries
        no ``last_tool_at`` key at all.  The field's own ``default_factory``
        would fill that gap with ``now()`` — exactly the bug this field
        exists to fix, since a long-dead session would then read as freshly
        active.  The record's own ``last_active`` is the best available
        approximation of when it was last touched, so backfill from that
        instead of the field default.  A genuinely fresh construction (no
        ``last_active`` in the input either) falls through to both fields'
        own ``default_factory`` — the correct "session just started" value.
        """
        if not isinstance(data, dict):
            return data
        payload = cast("dict[str, object]", data)
        if "last_tool_at" not in payload and "last_active" in payload:
            return {**payload, "last_tool_at": payload["last_active"]}
        return payload

    @field_validator("last_active", "last_tool_at", mode="after")
    @classmethod
    def _normalize_activity_timestamps(cls, v: datetime) -> datetime:
        return _ensure_utc(v)

    def is_live(self, *, now: datetime, ttl_seconds: float) -> bool:
        """True if the last heartbeat is recent enough to consider live.

        A running server heartbeats on a fixed interval, refreshing
        ``last_active``.  A session whose last heartbeat is older than
        *ttl_seconds* has stopped heartbeating (shut down, killed, or
        wedged) and is not live, even though its KV entry may not have
        hit the longer storage TTL yet.
        """
        return (now - self.last_active).total_seconds() <= ttl_seconds


@dataclass(frozen=True)
class RelayAuth:
    """Authentication credentials for a remote NATS relay.

    At most one field may be set.  Mutual exclusivity is enforced
    at config-parse time in :func:`~biff.config.extract_biff_fields`.
    """

    # repr=False on all three: a bare dataclass repr is a live secret leak
    # once BIFF_RELAY_TOKEN carries CI-sourced secrets through this object
    # (any accidental `logger.debug("%r", config)` or uncaught traceback
    # would otherwise print the token in clear text).
    token: str | None = field(default=None, repr=False)
    """Shared secret token."""

    nkeys_seed: str | None = field(default=None, repr=False)
    """Path to an NKey seed file (``.nk``)."""

    user_credentials: str | None = field(default=None, repr=False)
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


class RelayConnectError(ConnectionError):
    """Raised when ``nats.connect()`` fails, in place of the raw exception.

    Every call site that expands ``RelayAuth.as_nats_kwargs()`` into
    ``nats.connect()`` must raise this ``from None`` rather than let the
    original exception propagate -- the raw exception's traceback frame
    holds the plaintext auth kwargs dict via ``__context__``, which
    ``from None`` alone does not clear (only ``finally: auth_kwargs.clear()``
    at the call site does that).

    Subclasses the builtin ``ConnectionError`` (itself an ``OSError``)
    rather than ``Exception`` directly: existing best-effort callers along
    the talk/REPL path already catch ``(NatsError, TimeoutError, OSError)``
    around calls that indirectly redial through ``_ensure_connected()``.
    Raising a bare ``Exception`` subclass here would silently fall through
    those handlers and crash a loop that used to absorb the failure and
    keep going.
    """


class BiffConfig(BaseModel):
    """Validated biff configuration.

    Config comes from ``.punt-labs/biff/config.yaml`` (shared) and
    ``config.local.yaml`` (local overrides), or from zero-config
    git-remote derivation.  Parsing is handled by ``config.py``;
    this model holds the validated result.
    """

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    user: str = Field(min_length=1)
    display_name: str = ""
    kind: str = Field(default="", description="Identity kind: human, agent, or empty")
    repo_name: str = Field(min_length=1)
    relay_url: str | None = None
    relay_auth: RelayAuth | None = None
    relay_tls_handshake_first: bool = False
    """Start TLS immediately on connect instead of negotiating.

    ``tls://`` alone does not say who speaks first: a native-TLS
    ``nats-server`` (the demo relay's ``connect.ngs.global``) sends a
    plaintext INFO line and upgrades opportunistically, while a
    TLS-terminating proxy (a load balancer's TLS listener in front of a
    plaintext ``nats-server``) expects a TLS ClientHello as the very
    first bytes and never sends that plaintext preamble. The two are
    indistinguishable from the URL, so this is an explicit operator
    opt-in (``relay.tls_handshake_first: true``) for the proxy case, not
    inferred from the scheme.
    """
    team: tuple[str, ...] = ()
    peers: tuple[str, ...] = ()
    orgs: tuple[str, ...] = ()
    poll_interval: float = 2.0

    @property
    def visible_repos(self) -> frozenset[str]:
        """Repos visible to this instance: self + explicit peers.

        Does NOT include org-discovered repos — those are resolved at
        runtime via ``NatsRelay.discover_repos_for_org()`` and merged
        by the caller (app state or CLI session).
        """
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
