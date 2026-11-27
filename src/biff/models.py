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
from typing import Annotated, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)


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


PLAN_MAX_LENGTH = 512
"""Longest accepted plan; matches ``Message.body`` and ``WallPost.text``."""
_PARTY_MAX_LENGTH = 128  # a session id's limit: the longest user or tty name
_ENVELOPE_MAX_LENGTH = 256  # one encryption-envelope field of a message

_PLAN_TEXT: TypeAdapter[str] = TypeAdapter(
    Annotated[str, Field(max_length=PLAN_MAX_LENGTH)]
)


def validate_plan(text: str) -> str:
    """Return *text* if it fits :data:`PLAN_MAX_LENGTH`, else raise.

    Empty is valid: an empty plan clears the status.  Raises
    :class:`pydantic.ValidationError` when *text* is too long.

    Use this for plan text a caller supplied directly; for machine-generated
    hints that must not fail, :func:`truncate_plan` instead.
    """
    return _PLAN_TEXT.validate_python(text)


def truncate_plan(text: str) -> str:
    """Clip machine-generated plan text to fit :data:`PLAN_MAX_LENGTH`.

    Generated hints (a branch name, a commit subject) are not user-supplied
    plans and must never reach the reject path — a long branch would otherwise
    fail its own automatic ``/plan`` and leave the plan cleared.  An over-long
    value is cut to ``PLAN_MAX_LENGTH`` with a trailing ellipsis; a value that
    already fits is returned unchanged.
    """
    if len(text) <= PLAN_MAX_LENGTH:
        return text
    return text[: PLAN_MAX_LENGTH - 1] + "…"


class Message(BaseModel):
    """A single async message between two users.

    Stored in JSONL format in the user's inbox. Once created, messages
    are immutable — the ``read`` flag is tracked by the storage layer
    rather than by mutating the message in place.
    """

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    id: uuid.UUID = Field(default_factory=_new_id)
    from_user: str = Field(min_length=1, max_length=_PARTY_MAX_LENGTH)
    from_tty: str = Field(default="", max_length=_PARTY_MAX_LENGTH)  # sender's tty name
    to_user: str = Field(min_length=1, max_length=_PARTY_MAX_LENGTH)
    body: str = Field(min_length=1, max_length=512)
    timestamp: datetime = Field(default_factory=_utc_now)
    read: bool = False
    # Encryption envelope, reserved for a future end-to-end encryption
    # feature (DES-016).  All defaults are empty/false; populated when
    # E2E encryption is active.
    encrypted: bool = False
    nonce: str = Field(default="", max_length=_ENVELOPE_MAX_LENGTH)
    sender_pubkey: str = Field(default="", max_length=_ENVELOPE_MAX_LENGTH)
    encryption_mode: str = Field(default="", max_length=_ENVELOPE_MAX_LENGTH)

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
    Conflating the two would make an inactive session appear freshly active
    because the heartbeat keeps last_active perpetually fresh.
    """

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    user: str = Field(min_length=1)
    tty: str = Field(default="", description="8-char hex session identifier")
    tty_name: str = Field(default="", description="Human-readable session name")
    hostname: str = ""
    pwd: str = ""
    display_name: str = ""
    kind: str = Field(default="", description="Identity kind: human, agent, or empty")
    launched_by: str = Field(
        default="",
        description="OS user of the human who launched the process, or empty",
    )
    owner_git: str = Field(
        default="",
        description="BIFF_OWNER, else git user.name, at registration, or empty",
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
        """Backfill a missing last_tool_at from the record's last_active value.

        Using the field default would make an old stored session appear freshly
        active. Fresh constructions without either timestamp use both defaults.
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

    def beat(self, last_tool_at: datetime | None = None) -> UserSession:
        """Return this row heartbeated: alive now, last tool call never moved back."""
        now = datetime.now(UTC)
        newest = max(self.last_tool_at, last_tool_at or self.last_tool_at)
        return self.model_copy(update={"last_active": now, "last_tool_at": newest})

    def is_live(self, *, now: datetime, ttl_seconds: float) -> bool:
        """Check whether the last heartbeat is recent enough to consider live.

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
    those handlers instead of allowing the loop to recover.
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
    launched_by: str = Field(
        default="",
        description="OS user who launched this process, or empty when unknown",
    )
    owner_git: str = Field(
        default="",
        description="BIFF_OWNER, else git user.name, at startup, or empty if unset",
    )
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
    """Summarize unread counts for dynamic tool descriptions.

    Count-only (DES-015): no message preview. The count drives the read_messages
    description and tools/list_changed notification without creating consumers
    on the polling path.
    """

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    count: int = Field(default=0, ge=0)
