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

from biff.models import Message, UnreadSummary, UserSession

if TYPE_CHECKING:
    from nats.aio.client import Client as NatsClient
    from nats.js.client import JetStreamContext
    from nats.js.kv import KeyValue

logger = logging.getLogger(__name__)

_STREAM_NAME = "BIFF_INBOX"
_SUBJECT_PREFIX = "biff.inbox"
_KV_BUCKET = "biff-sessions"
_KV_TTL = 300  # seconds — buffer beyond default session TTL
_FETCH_BATCH = 100
_FETCH_TIMEOUT = 1.0
_PEEK_TIMEOUT = 0.5
_MAX_PREVIEW_LEN = 80
_MAX_BODY_PREVIEW = 40
_MAX_PREVIEW_MESSAGES = 3


class NatsRelay:
    """NATS-backed relay with JetStream messages and KV sessions.

    All infrastructure (KV bucket, stream) is provisioned lazily on the
    first method call via :meth:`_ensure_connected`.  Repeated calls are
    idempotent.
    """

    def __init__(self, url: str = "nats://localhost:4222") -> None:
        self._url = url
        self._nc: NatsClient | None = None
        self._js: JetStreamContext | None = None
        self._kv: KeyValue | None = None

    async def _ensure_connected(self) -> tuple[JetStreamContext, KeyValue]:
        """Lazily connect and provision infrastructure."""
        if self._js is not None and self._kv is not None:
            return self._js, self._kv
        nc = await nats.connect(self._url)  # pyright: ignore[reportUnknownMemberType]
        self._nc = nc
        js = nc.jetstream()  # pyright: ignore[reportUnknownMemberType]
        self._js = js

        # KV bucket for sessions — TTL auto-purges truly stale entries
        self._kv = await js.create_key_value(  # pyright: ignore[reportUnknownMemberType]
            config=KeyValueConfig(bucket=_KV_BUCKET, ttl=_KV_TTL),
        )

        # Stream for messages — WORK_QUEUE deletes on ack (POP semantics)
        await js.add_stream(  # pyright: ignore[reportUnknownMemberType]
            config=StreamConfig(
                name=_STREAM_NAME,
                subjects=[f"{_SUBJECT_PREFIX}.>"],
                retention=RetentionPolicy.WORK_QUEUE,
            ),
        )

        return js, self._kv

    async def close(self) -> None:
        """Close the NATS connection and release resources."""
        if self._nc is not None:
            await self._nc.close()
            self._nc = None
            self._js = None
            self._kv = None

    # -- Messages --

    async def deliver(self, message: Message) -> None:
        """Publish a message to the recipient's JetStream subject."""
        js, _ = await self._ensure_connected()
        subject = f"{_SUBJECT_PREFIX}.{message.to_user}"
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
        js, _ = await self._ensure_connected()
        subject = f"{_SUBJECT_PREFIX}.{user}"

        # Get per-subject count from stream info
        try:
            info = await js.stream_info(_STREAM_NAME, subjects_filter=subject)
        except NotFoundError:
            return UnreadSummary()

        count = 0
        if info.state.subjects:
            count = info.state.subjects.get(subject, 0)
        if count == 0:
            return UnreadSummary()

        # Peek via the same durable consumer — nak puts messages back
        sub = await js.pull_subscribe(
            subject, durable=self._durable_name(user), stream=_STREAM_NAME
        )
        try:
            raw_msgs = await sub.fetch(
                batch=min(count, _MAX_PREVIEW_MESSAGES),
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

        previews = [
            f"@{m.from_user} about {m.body[:_MAX_BODY_PREVIEW]}"
            for m in messages[:_MAX_PREVIEW_MESSAGES]
        ]
        preview = ", ".join(previews)
        if len(preview) > _MAX_PREVIEW_LEN:
            preview = preview[: _MAX_PREVIEW_LEN - 3] + "..."
        return UnreadSummary(count=count, preview=preview)

    # -- Presence --

    async def update_session(self, session: UserSession) -> None:
        """Store session in KV — ``put()`` resets the TTL."""
        _, kv = await self._ensure_connected()
        await kv.put(session.user, session.model_dump_json().encode())

    async def get_session(self, user: str) -> UserSession | None:
        """Read a single session from KV."""
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
        _, kv = await self._ensure_connected()
        try:
            entry = await kv.get(user)
            if entry.value is None:
                raise KeyNotFoundError
            existing = UserSession.model_validate_json(entry.value)
            updated = existing.model_copy(update={"last_active": datetime.now(UTC)})
        except (KeyNotFoundError, BucketNotFoundError):
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
