"""NATS-backed relay using JetStream for messages and KV for sessions.

``NatsRelay`` implements the :class:`~biff.relay.Relay` protocol using:

- **NATS KV** (``biff-sessions`` bucket) for session/presence data with
  TTL-based expiry.
- **NATS JetStream** (``BIFF_INBOX`` stream) with ``WORK_QUEUE`` retention
  for POP message semantics.

Messages are consumed (deleted) on :meth:`fetch`; :meth:`mark_read` is a
no-op.  :meth:`get_unread_summary` peeks at messages non-destructively
using stream info and durable consumers with nak.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

import nats
from nats.js.api import (
    KeyValueConfig,
    RetentionPolicy,
    StreamConfig,
)
from nats.js.errors import (
    BucketNotFoundError,
    KeyNotFoundError,
    NoKeysError,
    NotFoundError,
)
from pydantic import ValidationError

from biff.models import Message, RelayAuth, UserSession, build_unread_summary

if TYPE_CHECKING:
    from nats.aio.client import Client as NatsClient
    from nats.js.client import JetStreamContext
    from nats.js.kv import KeyValue

    from biff.models import UnreadSummary

logger = logging.getLogger(__name__)

_STREAM_NAME = "BIFF_INBOX"
_SUBJECT_PREFIX = "biff.inbox"
_KV_BUCKET = "biff-sessions"
_KV_TTL = 300  # seconds — buffer beyond default session TTL
_KV_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB — small JSON session blobs
_STREAM_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB — messages consumed on read
_FETCH_BATCH = 100
_FETCH_TIMEOUT = 1.0
_PEEK_TIMEOUT = 0.5
_PEEK_BATCH = 3


class NatsRelay:
    """NATS-backed relay with JetStream messages and KV sessions.

    All infrastructure (KV bucket, stream) is provisioned lazily on the
    first method call via :meth:`_ensure_connected`.  Repeated calls are
    idempotent.
    """

    def __init__(
        self,
        url: str = "nats://localhost:4222",
        auth: RelayAuth | None = None,
        name: str = "biff",
    ) -> None:
        self._url = url
        self._auth = auth
        self._name = name
        self._nc: NatsClient | None = None
        self._js: JetStreamContext | None = None
        self._kv: KeyValue | None = None

    def _auth_kwargs(self) -> dict[str, str]:
        """Build authentication keyword arguments for ``nats.connect()``."""
        if self._auth is None:
            return {}
        if self._auth.token:
            return {"token": self._auth.token}
        if self._auth.nkeys_seed:
            return {"nkeys_seed": self._auth.nkeys_seed}
        if self._auth.user_credentials:
            return {"user_credentials": self._auth.user_credentials}
        return {}

    async def _ensure_connected(self) -> tuple[JetStreamContext, KeyValue]:
        """Lazily connect and provision infrastructure.

        Sets instance attributes only after all provisioning succeeds,
        ensuring no connection leak if KV/stream creation fails.
        """
        if self._js is not None and self._kv is not None:
            return self._js, self._kv

        async def _on_disconnect() -> None:
            logger.warning("Disconnected from NATS at %s", self._url)

        async def _on_reconnect() -> None:
            logger.info("Reconnected to NATS at %s", self._url)

        async def _on_error(exc: Exception) -> None:
            logger.error("NATS error: %s", exc)

        nc = await nats.connect(  # pyright: ignore[reportUnknownMemberType]
            self._url,
            name=self._name,
            disconnected_cb=_on_disconnect,
            reconnected_cb=_on_reconnect,
            error_cb=_on_error,
            **self._auth_kwargs(),
        )
        try:
            js = nc.jetstream()  # pyright: ignore[reportUnknownMemberType]

            # KV bucket for sessions — TTL auto-purges truly stale entries
            kv = await js.create_key_value(  # pyright: ignore[reportUnknownMemberType]
                config=KeyValueConfig(
                    bucket=_KV_BUCKET,
                    ttl=_KV_TTL,
                    max_bytes=_KV_MAX_BYTES,
                ),
            )

            # Stream for messages — WORK_QUEUE deletes on ack (POP semantics)
            await js.add_stream(  # pyright: ignore[reportUnknownMemberType]
                config=StreamConfig(
                    name=_STREAM_NAME,
                    subjects=[f"{_SUBJECT_PREFIX}.>"],
                    retention=RetentionPolicy.WORK_QUEUE,
                    max_bytes=_STREAM_MAX_BYTES,
                ),
            )
        except Exception:
            await nc.close()
            raise

        self._nc = nc
        self._js = js
        self._kv = kv
        return js, kv

    def reset_infrastructure(self) -> None:
        """Clear cached KV/stream handles, forcing re-provisioning.

        Call after external deletion of the KV bucket or stream
        (e.g. test cleanup) to ensure the next operation re-creates
        them.  The underlying NATS connection is preserved.
        """
        self._js = None
        self._kv = None

    async def close(self) -> None:
        """Close the NATS connection and release resources."""
        if self._nc is not None:
            await self._nc.close()
            self._nc = None
            self._js = None
            self._kv = None

    @staticmethod
    def _validate_user(user: str) -> str:
        """Reject usernames that could escape NATS subject boundaries.

        NATS subjects use ``.`` as a separator and ``*``/``>`` as
        wildcards.  Allowing these in usernames would let a crafted
        name match unintended subjects.
        """
        if not user or any(c in user for c in (".", "*", ">", " ")):
            msg = f"Invalid username: {user!r}"
            raise ValueError(msg)
        return user

    # -- Messages --

    async def deliver(self, message: Message) -> None:
        """Publish a message to the recipient's JetStream subject."""
        self._validate_user(message.from_user)
        js, _ = await self._ensure_connected()
        subject = f"{_SUBJECT_PREFIX}.{self._validate_user(message.to_user)}"
        await js.publish(subject, message.model_dump_json().encode())

    def _durable_name(self, user: str) -> str:
        """Durable consumer name for a user's inbox.

        WORK_QUEUE streams allow only one consumer per filter subject.
        Using a durable consumer lets repeated calls reuse the same
        server-side consumer instead of racing with ephemeral cleanup.
        """
        return f"inbox-{user}"

    async def fetch(self, user: str) -> list[Message]:
        """Pull and ack all messages — WORK_QUEUE deletes them on ack."""
        self._validate_user(user)
        js, _ = await self._ensure_connected()
        subject = f"{_SUBJECT_PREFIX}.{user}"

        sub = await js.pull_subscribe(
            subject, durable=self._durable_name(user), stream=_STREAM_NAME
        )
        try:
            raw_msgs = await sub.fetch(batch=_FETCH_BATCH, timeout=_FETCH_TIMEOUT)
        except TimeoutError:
            raw_msgs = []
        finally:
            await sub.unsubscribe()

        messages: list[Message] = []
        for raw in raw_msgs:
            try:
                msg = Message.model_validate_json(raw.data)
                messages.append(msg)
            except (ValidationError, ValueError):
                logger.warning("Skipping malformed NATS message on %s", subject)
            await raw.ack()
        return messages

    async def mark_read(self, user: str, ids: Sequence[UUID]) -> None:
        """No-op — messages are consumed (deleted) by :meth:`fetch`."""

    async def get_unread_summary(self, user: str) -> UnreadSummary:
        """Peek at messages non-destructively for notification preview."""
        self._validate_user(user)
        js, _ = await self._ensure_connected()
        subject = f"{_SUBJECT_PREFIX}.{user}"

        # Get per-subject count from stream info
        try:
            info = await js.stream_info(_STREAM_NAME, subjects_filter=subject)
        except NotFoundError:
            return build_unread_summary([], 0)

        count = 0
        if info.state.subjects:
            count = info.state.subjects.get(subject, 0)
        if count == 0:
            return build_unread_summary([], 0)

        # Peek via the same durable consumer — nak puts messages back
        sub = await js.pull_subscribe(
            subject, durable=self._durable_name(user), stream=_STREAM_NAME
        )
        try:
            raw_msgs = await sub.fetch(
                batch=min(count, _PEEK_BATCH),
                timeout=_PEEK_TIMEOUT,
            )
        except TimeoutError:
            raw_msgs = []
        finally:
            await sub.unsubscribe()

        messages: list[Message] = []
        for raw in raw_msgs:
            with suppress(ValidationError, ValueError):
                messages.append(Message.model_validate_json(raw.data))
            await raw.nak()  # Put back — don't consume

        return build_unread_summary(messages, count)

    # -- Presence --

    async def update_session(self, session: UserSession) -> None:
        """Store session in KV — ``put()`` resets the TTL."""
        self._validate_user(session.user)
        _, kv = await self._ensure_connected()
        await kv.put(session.user, session.model_dump_json().encode())

    async def get_session(self, user: str) -> UserSession | None:
        """Read a single session from KV."""
        self._validate_user(user)
        _, kv = await self._ensure_connected()
        try:
            entry = await kv.get(user)
            if entry.value is None:
                return None
            return UserSession.model_validate_json(entry.value)
        except (KeyNotFoundError, BucketNotFoundError):
            return None

    async def heartbeat(self, user: str) -> None:
        """Update ``last_active``, creating session if needed."""
        self._validate_user(user)
        _, kv = await self._ensure_connected()
        try:
            entry = await kv.get(user)
            if entry.value is None:
                raise KeyNotFoundError
            existing = UserSession.model_validate_json(entry.value)
            updated = existing.model_copy(update={"last_active": datetime.now(UTC)})
        except (KeyNotFoundError, BucketNotFoundError, ValidationError, ValueError):
            updated = UserSession(user=user)
        await kv.put(user, updated.model_dump_json().encode())

    async def get_active_sessions(self, *, ttl: int = 120) -> list[UserSession]:
        """Return sessions active within the TTL window."""
        _, kv = await self._ensure_connected()
        cutoff = datetime.now(UTC) - timedelta(seconds=ttl)
        sessions: list[UserSession] = []
        try:
            keys = await kv.keys()  # pyright: ignore[reportUnknownMemberType]
        except (NotFoundError, BucketNotFoundError, NoKeysError):
            return []
        for key in keys:
            try:
                entry = await kv.get(key)
                if entry.value is None:
                    continue
                session = UserSession.model_validate_json(entry.value)
                if session.last_active >= cutoff:
                    sessions.append(session)
            except (KeyNotFoundError, ValidationError, ValueError):
                continue
        return sessions
