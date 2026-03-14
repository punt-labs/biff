"""NATS-backed relay using JetStream for messages and KV for sessions.

``NatsRelay`` implements the :class:`~biff.relay.Relay` protocol using:

- **NATS KV** (``biff-sessions`` bucket) for session/presence data
  with TTL-based expiry.  Keys are ``{repo}.{user}.{tty}``.
- **NATS JetStream** (``biff-inbox`` stream) with ``WORK_QUEUE``
  retention for POP message semantics.  Two subject levels:

  - User inbox: ``biff.{repo}.inbox.{user}`` (broadcast)
  - TTY inbox:  ``biff.{repo}.inbox.{user}.{tty}`` (targeted)

Three shared streams total (DES-016).  Repos are isolated by subject
hierarchy (``biff.{repo}.inbox.>``) and KV key prefix (``{repo}.``),
not by separate NATS resources.  This eliminates the per-repo stream
scaling wall (25-stream Synadia Cloud limit hit at 8 repos).

Messages are consumed (deleted) on :meth:`fetch`; :meth:`mark_read` is a
no-op.  :meth:`get_unread_summary` uses ``stream_info()`` for counts
only — zero consumers created (DES-015).
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import nats
from nats.errors import Error as NatsError
from nats.js.api import (
    ConsumerConfig,
    DeliverPolicy,
    KeyValueConfig,
    RetentionPolicy,
    StreamConfig,
)
from nats.js.errors import (
    BadRequestError,
    BucketNotFoundError,
    KeyNotFoundError,
    NotFoundError,
)
from pydantic import ValidationError

from biff.models import (
    Message,
    RelayAuth,
    SessionEvent,
    UnreadSummary,
    UserSession,
    WallPost,
)
from biff.relay import SESSION_TTL_SECONDS
from biff.tty import build_session_key

if TYPE_CHECKING:
    from nats.aio.client import Client as NatsClient
    from nats.js.client import JetStreamContext
    from nats.js.kv import KeyValue

logger = logging.getLogger(__name__)

_KV_TTL = SESSION_TTL_SECONDS  # NATS auto-expires keys not refreshed within this window
_KV_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB — shared bucket, all repos' sessions
_STREAM_MAX_BYTES = 100 * 1024 * 1024  # 100 MiB — shared stream, all repos' messages
_FETCH_BATCH = 100
_FETCH_TIMEOUT = 1.0
_WTMP_MAX_AGE = 30 * 24 * 60 * 60  # 30 days in seconds
_CONSUMER_INACTIVE_THRESHOLD = 300.0  # 5 min — dead sessions auto-expire

# Default stream prefix (DES-016).  Tests override via stream_prefix="biff-dev".
_DEFAULT_STREAM_PREFIX = "biff"

# KV key namespaces reserved for non-session data (DES-016, DES-030).
# Session keys are {user}.{tty}; these first-segment values are not users.
# "wall" — wall keys are {repo}.wall (2 parts, same shape as sessions).
# "key" — encryption key reservations.
RESERVED_KV_NAMESPACES: frozenset[str] = frozenset({"key", "wall"})


async def safe_close(nc: NatsClient) -> None:
    """Close a NATS connection, suppressing Python 3.14+ SSL teardown errors.

    Python 3.14 raises ``ssl.SSLError: APPLICATION_DATA_AFTER_CLOSE_NOTIFY``
    when the server sends data after the TLS close_notify.  This is harmless
    during intentional teardown — the connection is going away regardless.
    """
    try:
        await nc.close()
    except ssl.SSLError as exc:
        if "APPLICATION_DATA_AFTER_CLOSE_NOTIFY" not in str(exc):
            raise


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
        repo_name: str = "_default",
        stream_prefix: str = _DEFAULT_STREAM_PREFIX,
    ) -> None:
        self._url = url
        self._auth = auth
        self._name = name
        self._repo_name = repo_name
        # Validate stream_prefix — must be a simple alphanumeric-dash token.
        # Dots, wildcards, or spaces would break NATS subject routing.
        if not stream_prefix or not all(c.isalnum() or c == "-" for c in stream_prefix):
            msg = (
                f"stream_prefix must be non-empty alphanumeric-dash: {stream_prefix!r}"
            )
            raise ValueError(msg)
        # Shared resource names (DES-016) — constant across repos.
        # stream_prefix allows tests to isolate from production streams.
        self._stream_name = f"{stream_prefix}-inbox"
        self._kv_bucket = f"{stream_prefix}-sessions"
        self._wtmp_stream = f"{stream_prefix}-wtmp"
        # Subject prefixes — repo-specific within shared streams.
        self._stream_prefix = stream_prefix
        self._subject_prefix = f"{stream_prefix}.{repo_name}.inbox"
        self._wtmp_prefix = f"{stream_prefix}.{repo_name}.wtmp"
        self._nc: NatsClient | None = None
        self._js: JetStreamContext | None = None
        self._kv: KeyValue | None = None
        self._connect_lock = asyncio.Lock()
        self._wtmp_available: bool = False

    def _auth_kwargs(self) -> dict[str, str]:
        """Build authentication keyword arguments for ``nats.connect()``."""
        if self._auth is None:
            return {}
        return self._auth.as_nats_kwargs()

    def _cached_handles(
        self,
    ) -> tuple[JetStreamContext, KeyValue] | None:
        """Return cached handles if connection is alive, else None."""
        js, kv, nc = self._js, self._kv, self._nc
        if js is not None and kv is not None and nc is not None and not nc.is_closed:
            return js, kv
        return None

    async def _ensure_connected(self) -> tuple[JetStreamContext, KeyValue]:
        """Lazily connect and provision infrastructure.

        Reuses an existing NATS connection if available (e.g. after
        :meth:`reset_infrastructure`).  Only creates a new connection
        when none exists or the previous one was closed.
        """
        # Lock-free fast path: return cached handles if connection is alive.
        cached = self._cached_handles()
        if cached is not None:
            return cached

        # Slow path: serialize connection creation to prevent concurrent
        # callers from each creating a separate NATS connection (DES-029).
        async with self._connect_lock:
            # Double-check after acquiring the lock — another caller may
            # have already reconnected while we waited.
            cached = self._cached_handles()
            if cached is not None:
                return cached
            # Connection died or never existed — clear stale handles.
            self._js = None
            self._kv = None
            return await self._open_connection()

    async def _open_connection(self) -> tuple[JetStreamContext, KeyValue]:
        """Create a new NATS connection and provision infrastructure.

        Must be called while holding ``_connect_lock``.
        """
        nc = self._nc
        if nc is None or nc.is_closed:

            async def _on_disconnect() -> None:
                logger.warning("Disconnected from NATS at %s", self._url)
                # Proactively invalidate cached handles so the next
                # tool call reconnects instead of using stale
                # JetStream/KV refs (DES-029).
                self._js = None
                self._kv = None

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

            # KV bucket for sessions — shared across all repos (DES-016).
            # TTL auto-purges truly stale entries.  Never delete-recreate
            # shared infrastructure; use the existing bucket on config
            # mismatch.
            kv_config = KeyValueConfig(
                bucket=self._kv_bucket,
                ttl=_KV_TTL,
                max_bytes=_KV_MAX_BYTES,
            )
            try:
                kv = await js.create_key_value(  # pyright: ignore[reportUnknownMemberType]
                    config=kv_config,
                )
            except BadRequestError:
                logger.info(
                    "Shared KV bucket %s config differs, using as-is",
                    self._kv_bucket,
                )
                kv = await js.key_value(self._kv_bucket)  # pyright: ignore[reportUnknownMemberType]

            # Inbox stream — shared WORK_QUEUE with wildcard subjects
            # (DES-016).  Never delete-recreate shared infrastructure.
            stream_config = StreamConfig(
                name=self._stream_name,
                subjects=[f"{self._stream_prefix}.*.inbox.>"],
                retention=RetentionPolicy.WORK_QUEUE,
                max_bytes=_STREAM_MAX_BYTES,
            )
            try:
                await js.add_stream(config=stream_config)  # pyright: ignore[reportUnknownMemberType]
            except BadRequestError:
                logger.info(
                    "Shared stream %s config differs, using as-is",
                    self._stream_name,
                )

            await self._provision_wtmp(js)
            await self._cleanup_legacy_streams(js)
        except Exception:
            await safe_close(nc)
            raise

        self._nc = nc
        self._js = js
        self._kv = kv
        return js, kv

    async def _provision_wtmp(self, js: JetStreamContext) -> None:
        """Provision the shared wtmp stream, degrading gracefully on failure.

        Non-fatal: any provisioning failure disables wtmp but leaves
        core messaging fully operational.  Never delete-recreate
        shared infrastructure (DES-016).
        """
        wtmp_config = StreamConfig(
            name=self._wtmp_stream,
            subjects=[f"{self._stream_prefix}.*.wtmp.>"],
            retention=RetentionPolicy.LIMITS,
            max_bytes=_STREAM_MAX_BYTES,
            max_age=_WTMP_MAX_AGE,
        )
        try:
            await js.add_stream(config=wtmp_config)  # pyright: ignore[reportUnknownMemberType]
            self._wtmp_available = True
        except BadRequestError as exc:
            if "maximum number of streams" in str(exc):
                logger.warning("Wtmp stream unavailable: %s", exc)
                self._wtmp_available = False
            else:
                # Shared stream config differs — use as-is.
                logger.info(
                    "Shared wtmp stream %s config differs, using as-is",
                    self._wtmp_stream,
                )
                self._wtmp_available = True
        except Exception:  # noqa: BLE001 — provisioning must never crash startup
            logger.warning("Wtmp stream provisioning failed", exc_info=True)
            self._wtmp_available = False

    async def _cleanup_legacy_streams(self, js: JetStreamContext) -> None:
        """Delete orphaned per-repo streams from pre-DES-016 installations.

        Best-effort: any failure is logged and swallowed.  Legacy cleanup
        must never crash startup.  Always runs — no migration flag.
        Each delete is a no-op (suppressed NotFoundError) once legacy
        resources are gone.
        """
        try:
            for name in (
                f"biff-{self._repo_name}-inbox",
                f"biff-{self._repo_name}-wtmp",
            ):
                with suppress(NotFoundError):
                    await js.delete_stream(name)  # pyright: ignore[reportUnknownMemberType]
                    logger.info("Cleaned up legacy stream %s", name)
            with suppress(NotFoundError, BucketNotFoundError):
                await js.delete_key_value(  # pyright: ignore[reportUnknownMemberType]
                    f"biff-{self._repo_name}-sessions",
                )
                logger.info(
                    "Cleaned up legacy KV bucket biff-%s-sessions",
                    self._repo_name,
                )
        except Exception:  # noqa: BLE001 — best-effort cleanup must never crash startup
            logger.warning("Legacy stream cleanup failed", exc_info=True)

    @property
    def wtmp_available(self) -> bool:
        """Whether the wtmp stream was successfully provisioned."""
        return self._wtmp_available

    async def get_kv(self) -> KeyValue:
        """Return the NATS KV handle, connecting if necessary."""
        _, kv = await self._ensure_connected()
        return kv

    def reset_infrastructure(self) -> None:
        """Clear cached KV/stream handles, forcing re-provisioning.

        Call after external deletion of the KV bucket or stream
        (e.g. test cleanup) to ensure the next operation re-creates
        them.  The underlying NATS connection is preserved.
        """
        self._js = None
        self._kv = None
        self._wtmp_available = False

    async def purge_data(self) -> None:
        """Purge this repo's sessions and data from shared infrastructure.

        Session KV keys are org-scoped (``{user}.{tty}``, DES-030).
        To purge only this repo's sessions, we fetch all sessions,
        filter by ``repo == self._repo_name``, and delete individually.
        Wall keys (``{repo}.wall``) are repo-scoped and deleted by key.
        Messages and wtmp remain repo-scoped via subject hierarchy.
        """
        js, kv = await self._ensure_connected()
        # Delete this repo's sessions individually (org-scoped KV keys
        # cannot be purged by subject filter).
        sessions = await self.get_sessions()
        for session in sessions:
            if session.repo == self._repo_name:
                key = self._kv_key(build_session_key(session.user, session.tty))
                with suppress(KeyNotFoundError, BucketNotFoundError):
                    await kv.delete(key)
        # Delete this repo's wall key
        with suppress(KeyNotFoundError, BucketNotFoundError):
            await kv.delete(self._wall_kv_key)
        with suppress(NotFoundError):
            await js.purge_stream(  # pyright: ignore[reportUnknownMemberType]
                self._stream_name, subject=f"{self._subject_prefix}.>"
            )
        with suppress(NotFoundError):
            await js.purge_stream(  # pyright: ignore[reportUnknownMemberType]
                self._wtmp_stream, subject=f"{self._wtmp_prefix}.>"
            )

    async def delete_infrastructure(self) -> None:
        """Purge this repo's data and clear cached handles.

        Shared streams are never deleted (DES-016) — other repos may
        be using them.  This is equivalent to :meth:`purge_data` plus
        clearing cached JetStream/KV handles to force re-provisioning.
        """
        await self.purge_data()
        self._js = None
        self._kv = None

    async def flush(self, *, timeout: int = 2) -> None:
        """Flush pending NATS publishes to the server.

        Ensures all buffered messages are sent before returning.
        Critical for shutdown paths where the process may exit
        immediately after.
        """
        if self._nc is not None and not self._nc.is_closed:
            await self._nc.flush(timeout=timeout)

    async def disconnect(self) -> None:
        """Release TCP connection temporarily.  Next relay call reconnects.

        Semantically distinct from :meth:`close` — disconnect is
        reversible (the session continues), close is permanent
        (the session is ending).
        """
        if self._nc is not None and not self._nc.is_closed:
            await safe_close(self._nc)
        self._nc = None
        self._js = None
        self._kv = None

    async def close(self) -> None:
        """Close the NATS connection and release resources."""
        if self._nc is not None:
            await safe_close(self._nc)
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

    @staticmethod
    def _validate_tty(tty: str) -> str:
        """Reject TTY values that would break NATS subjects or KV keys."""
        if not tty or any(c in tty for c in (".", "*", ">", " ", ":")):
            msg = f"Invalid tty: {tty!r}"
            raise ValueError(msg)
        return tty

    @staticmethod
    def _validated_sender_key(sender_key: str, from_user: str) -> str:
        """Return *sender_key* if well-formed and consistent, else ``""``."""
        if not sender_key:
            return ""
        parts = sender_key.split(":", maxsplit=1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return ""
        if parts[0] != from_user:
            return ""
        return sender_key

    def _subject_for_key(
        self, session_key: str, *, target_repo: str | None = None
    ) -> str:
        """NATS subject for a session key: ``biff.{repo}.inbox.{user}.{tty}``."""
        user, tty = session_key.split(":", maxsplit=1)
        self._validate_user(user)
        self._validate_tty(tty)
        repo = target_repo or self._repo_name
        prefix = f"{self._stream_prefix}.{repo}.inbox"
        return f"{prefix}.{user}.{tty}"

    def _user_subject(self, user: str) -> str:
        """NATS subject for a user's broadcast inbox: ``biff.{repo}.inbox.{user}``.

        3 tokens (distinct from the 4-token TTY subject).
        """
        self._validate_user(user)
        return f"{self._subject_prefix}.{user}"

    def _user_durable_name(self, user: str) -> str:
        """Durable consumer name for a user's broadcast inbox.

        Repo-prefixed (DES-016) to avoid collisions in shared streams.
        Uses ``userinbox-`` suffix to avoid collision with TTY durable
        names (``{repo}-inbox-{user}-{tty}``).
        """
        return f"{self._repo_name}-userinbox-{user}"

    def _kv_key(self, session_key: str) -> str:
        """KV key for a session: ``{user}.{tty}`` (DES-030, org-scoped)."""
        user, tty = session_key.split(":", maxsplit=1)
        self._validate_user(user)
        self._validate_tty(tty)
        return f"{user}.{tty}"

    @staticmethod
    def wall_kv_key(repo_name: str) -> str:
        """KV key for the team wall: ``{repo}.wall`` (DES-016)."""
        return f"{repo_name}.wall"

    @property
    def _wall_kv_key(self) -> str:
        """Instance shorthand for :meth:`wall_kv_key`."""
        return self.wall_kv_key(self._repo_name)

    def talk_notify_subject(self, user: str, *, target_repo: str | None = None) -> str:
        """NATS core subject for talk notifications to a user.

        Published by :meth:`deliver` after every message delivery.
        Subscribed by ``talk_listen`` for instant wake-up.  Core NATS
        (no stream) — messages are dropped if nobody is listening.
        """
        self._validate_user(user)
        repo = target_repo or self._repo_name
        return f"{self._stream_prefix}.{repo}.talk.notify.{user}"

    async def get_nc(self) -> NatsClient:
        """Return the raw NATS client, connecting if necessary.

        Used by talk tools for core pub/sub subscriptions that
        don't go through JetStream.
        """
        await self._ensure_connected()
        if self._nc is None:  # pragma: no cover — _ensure_connected guarantees this
            msg = "NATS client not available after connect"
            raise RuntimeError(msg)
        return self._nc

    # -- Messages --

    async def deliver(
        self,
        message: Message,
        *,
        sender_key: str = "",
        target_repo: str | None = None,
    ) -> None:
        """Publish a message to the recipient's JetStream subject.

        If ``to_user`` contains a ``:`` (targeted), publish to the
        TTY subject.  Otherwise (broadcast), publish to the user
        subject — no session lookup, persists offline.

        When *target_repo* is set, the message is published to that
        repo's subject hierarchy (cross-repo delivery, DES-030).

        After JetStream delivery, publishes a lightweight notification
        on a core NATS subject so any active ``talk_listen`` call wakes
        immediately.  The notification is best-effort — delivery succeeds
        even if notification fails.

        ``sender_key`` is the sender's session key (``user:tty``) so
        the notification payload can identify the originating session.
        Receivers use this to reject self-echo (same user, different tty).
        If ``sender_key`` fails validation (bad format, user mismatch),
        it is silently dropped rather than propagated.
        """
        self._validate_user(message.from_user)
        sender_key = self._validated_sender_key(sender_key, message.from_user)
        js, _ = await self._ensure_connected()

        if ":" in message.to_user:
            # Targeted delivery — TTY subject
            subject = self._subject_for_key(message.to_user, target_repo=target_repo)
            await js.publish(subject, message.model_dump_json().encode())
        else:
            # Broadcast — single user subject, no session lookup
            self._validate_user(message.to_user)
            if target_repo:
                prefix = f"{self._stream_prefix}.{target_repo}.inbox"
                subject = f"{prefix}.{message.to_user}"
            else:
                subject = self._user_subject(message.to_user)
            await js.publish(subject, message.model_dump_json().encode())

        # Notify any active talk_listen subscriber (core NATS, fire-and-forget).
        await self._publish_talk_notification(
            message.to_user, message, sender_key, target_repo=target_repo
        )

    async def _publish_talk_notification(
        self,
        to_user: str,
        message: Message | None = None,
        sender_key: str = "",
        *,
        target_repo: str | None = None,
    ) -> None:
        """Publish a talk notification so ``talk_listen`` wakes up.

        The payload carries the sender, message body, and sender session
        key so the status line poller can display incoming talk messages
        and reject self-echo.  Falls back to ``b"1"`` if no message.

        Best-effort: failures are logged at debug level and never
        propagate — the JetStream delivery (the critical path) has
        already succeeded.
        """
        if self._nc is None or self._nc.is_closed:
            return
        user = to_user.split(":")[0] if ":" in to_user else to_user
        try:
            subject = self.talk_notify_subject(user, target_repo=target_repo)
            if message is not None:
                data: dict[str, str] = {
                    "from": message.from_user,
                    "body": message.body,
                }
                if sender_key:
                    data["from_key"] = sender_key
                payload = json.dumps(data).encode()
            else:
                payload = b"1"
            await self._nc.publish(subject, payload)
        except Exception:  # noqa: BLE001 — notification is best-effort
            logger.debug("Talk notification failed for %s", user)

    def _durable_name(self, session_key: str) -> str:
        """Durable consumer name for a session's inbox.

        Repo-prefixed (DES-016) to avoid collisions in shared streams.
        WORK_QUEUE streams allow only one consumer per filter subject.
        Using a durable consumer lets repeated calls reuse the same
        server-side consumer instead of racing with ephemeral cleanup.
        """
        return f"{self._repo_name}-inbox-{session_key.replace(':', '-')}"

    async def _fetch_from_subject(
        self,
        js: JetStreamContext,
        subject: str,
        durable: str,
    ) -> list[Message]:
        """Pull, ack, and delete consumer for a WORK_QUEUE subject.

        Shared implementation for :meth:`fetch` (TTY inbox) and
        :meth:`fetch_user_inbox` (user broadcast inbox).  Acks are
        fire-and-forget in nats.py, so we flush before deleting the
        consumer to ensure the server has processed all acks.
        """
        sub = await js.pull_subscribe(
            subject,
            durable=durable,
            stream=self._stream_name,
            config=ConsumerConfig(inactive_threshold=_CONSUMER_INACTIVE_THRESHOLD),
        )
        try:
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

            # Acks are fire-and-forget publishes in nats.py.  Flush
            # ensures the server has received all acks before we delete
            # the consumer — otherwise the WORK_QUEUE message removal
            # can race with consumer deletion.
            if raw_msgs and self._nc is not None:
                await self._nc.flush()

            return messages
        finally:
            # Delete consumer after use — frees the server-side slot.
            # Broad except: any delete_consumer failure must not prevent
            # returning already-acked messages (WORK_QUEUE deletes on ack).
            # inactive_threshold provides backup cleanup.
            try:
                await js.delete_consumer(self._stream_name, durable)
            except (NatsError, TimeoutError):
                logger.debug("Consumer cleanup failed for %s (will expire)", durable)

    async def fetch(self, session_key: str) -> list[Message]:
        """Pull and ack all messages — WORK_QUEUE deletes them on ack."""
        js, _ = await self._ensure_connected()
        return await self._fetch_from_subject(
            js,
            subject=self._subject_for_key(session_key),
            durable=self._durable_name(session_key),
        )

    async def mark_read(self, session_key: str, ids: Sequence[UUID]) -> None:
        """No-op — messages are consumed (deleted) by :meth:`fetch`."""

    # -- Messages (user inbox) --

    async def fetch_user_inbox(self, user: str) -> list[Message]:
        """Pull and ack all messages from the user's broadcast inbox."""
        self._validate_user(user)
        js, _ = await self._ensure_connected()
        return await self._fetch_from_subject(
            js,
            subject=self._user_subject(user),
            durable=self._user_durable_name(user),
        )

    async def mark_read_user_inbox(self, user: str, ids: Sequence[UUID]) -> None:
        """No-op — messages are consumed (deleted) by :meth:`fetch_user_inbox`."""

    async def get_user_unread_count(self, user: str) -> int:
        """Count unread messages in the user's broadcast inbox."""
        self._validate_user(user)
        js, _ = await self._ensure_connected()
        subject = self._user_subject(user)
        try:
            info = await js.stream_info(self._stream_name, subjects_filter=subject)
        except NotFoundError:
            return 0
        if info.state.subjects:
            return info.state.subjects.get(subject, 0)
        return 0

    async def get_unread_summary(self, session_key: str) -> UnreadSummary:
        """Count unread messages across TTY and user inboxes.

        Uses ``stream_info()`` with subject filters — zero consumers
        created (DES-015).
        """
        js, _ = await self._ensure_connected()
        user = session_key.split(":")[0]
        tty_subject = self._subject_for_key(session_key)
        user_subject = self._user_subject(user)

        tty_count = 0
        user_count = 0
        try:
            tty_info = await js.stream_info(
                self._stream_name, subjects_filter=tty_subject
            )
            if tty_info.state.subjects:
                tty_count = tty_info.state.subjects.get(tty_subject, 0)
        except NotFoundError:
            pass
        try:
            user_info = await js.stream_info(
                self._stream_name, subjects_filter=user_subject
            )
            if user_info.state.subjects:
                user_count = user_info.state.subjects.get(user_subject, 0)
        except NotFoundError:
            pass
        return UnreadSummary(count=tty_count + user_count)

    # -- Presence --

    async def update_session(self, session: UserSession) -> None:
        """Store session in KV using ``{user}.{tty}`` key."""
        key = build_session_key(session.user, session.tty)
        kv_key = self._kv_key(key)
        _, kv = await self._ensure_connected()
        await kv.put(kv_key, session.model_dump_json().encode())

    async def get_session(self, session_key: str) -> UserSession | None:
        """Read a single session from KV by ``{user}:{tty}`` key."""
        kv_key = self._kv_key(session_key)
        _, kv = await self._ensure_connected()
        try:
            entry = await kv.get(kv_key)
            if entry.value is None:
                return None
            return UserSession.model_validate_json(entry.value)
        except (KeyNotFoundError, BucketNotFoundError):
            return None

    async def get_sessions_for_user(self, user: str) -> list[UserSession]:
        """Return all sessions for a given user."""
        self._validate_user(user)
        all_sessions = await self.get_sessions()
        return [s for s in all_sessions if s.user == user]

    async def heartbeat(self, session_key: str) -> None:
        """Update ``last_active`` for an existing session.

        If the session is missing from KV (expired, deleted, or not yet
        created), the heartbeat is skipped.  Writing a bare
        ``UserSession(user, tty)`` would destroy tty_name, plan,
        hostname, and other fields that only the lifespan or tool
        handlers know how to set.  The 3-day TTL means one skipped
        heartbeat is harmless; overwriting with a bare session is not.
        """
        kv_key = self._kv_key(session_key)
        _, kv = await self._ensure_connected()
        try:
            entry = await kv.get(kv_key)
            if entry.value is None:
                return  # No session to heartbeat
            existing = UserSession.model_validate_json(entry.value)
        except (KeyNotFoundError, BucketNotFoundError):
            return  # Session not found — nothing to heartbeat
        except (ValidationError, ValueError):
            logger.warning("Corrupt session for %s, skip heartbeat", session_key)
            return
        updated = existing.model_copy(update={"last_active": datetime.now(UTC)})
        await kv.put(kv_key, updated.model_dump_json().encode())

    async def get_sessions(self) -> list[UserSession]:
        """Return all sessions across the org (NATS KV TTL handles expiry).

        Uses ``stream_info`` with a wildcard ``subjects_filter`` to
        discover all KV keys.  Explicit structural filtering (DES-030)
        skips non-session keys (wall, encryption key reservations) by
        checking the first segment against ``RESERVED_KV_NAMESPACES``.

        Session keys are ``{user}.{tty}`` (2-part).  Wall keys are
        ``{repo}.wall`` (also 2-part but first segment matches a repo
        name, and "wall" is in RESERVED_KV_NAMESPACES).
        """
        js, kv = await self._ensure_connected()
        kv_stream = f"KV_{self._kv_bucket}"
        kv_prefix = f"$KV.{self._kv_bucket}."
        try:
            info = await js.stream_info(kv_stream, subjects_filter=f"{kv_prefix}>")
        except NotFoundError:
            return []
        if not info.state.subjects:
            return []
        # Collect session-shaped keys, filtering out non-session entries.
        session_keys: list[str] = []
        for subject in info.state.subjects:
            key = subject.removeprefix(kv_prefix)
            # Structural filter: session keys are {user}.{tty} (2 parts).
            # Skip keys where either segment is in RESERVED_KV_NAMESPACES
            # (wall keys like {repo}.wall, encryption keys like key.{user}).
            parts = key.split(".", maxsplit=1)
            if len(parts) != 2:
                continue
            if parts[0] in RESERVED_KV_NAMESPACES or parts[1] in RESERVED_KV_NAMESPACES:
                continue
            session_keys.append(key)

        # Fetch all session values concurrently to avoid N serial
        # round-trips (matters at 243+ concurrent sessions).
        async def _get_session(key: str) -> UserSession | None:
            try:
                entry = await kv.get(key)
                if entry.value is None:
                    return None
                return UserSession.model_validate_json(entry.value)
            except (KeyNotFoundError, ValidationError, ValueError):
                return None

        results = await asyncio.gather(*(_get_session(k) for k in session_keys))
        return [s for s in results if s is not None]

    async def delete_session(self, session_key: str) -> None:
        """Remove a session from KV storage and its inbox consumer."""
        kv_key = self._kv_key(session_key)
        js, kv = await self._ensure_connected()
        with suppress(KeyNotFoundError, BucketNotFoundError):
            await kv.delete(kv_key)
        # Delete the per-session inbox consumer.  fetch() already deletes
        # its consumer after use; this catches the case where a session ends
        # without a final fetch.  The user-level consumer (userinbox-{user})
        # is likewise deleted by fetch_user_inbox(); inactive_threshold is
        # the safety net for consumers orphaned by crashes.
        try:
            await js.delete_consumer(self._stream_name, self._durable_name(session_key))
        except NotFoundError:
            pass  # Already deleted by fetch() — expected.
        except (TimeoutError, NatsError) as exc:
            logger.warning(
                "Consumer cleanup failed for %s (will auto-expire): %s",
                session_key,
                exc,
            )

    # -- Session history (wtmp) --

    def _wtmp_subject(self, user: str) -> str:
        """NATS subject for a user's wtmp events: ``biff.{repo}.wtmp.{user}``."""
        self._validate_user(user)
        return f"{self._wtmp_prefix}.{user}"

    async def append_wtmp(self, event: SessionEvent) -> None:
        """Publish a session event to the wtmp stream."""
        js, _ = await self._ensure_connected()
        if not self._wtmp_available:
            return
        subject = self._wtmp_subject(event.user)
        await js.publish(subject, event.model_dump_json().encode())

    async def get_wtmp(
        self, *, user: str | None = None, count: int = 25
    ) -> list[SessionEvent]:
        """Fetch recent session events from the wtmp stream.

        Returns up to *count* events, most recent first.  When *user*
        is given, only events for that user are returned (filtered
        by NATS subject).
        """
        js, _ = await self._ensure_connected()
        if not self._wtmp_available:
            return []
        count = max(1, min(count, 1000))
        subject = self._wtmp_subject(user) if user else f"{self._wtmp_prefix}.>"

        # Fetch from the tail of the stream so we get the most recent
        # events, not the oldest.  Use opt_start_seq to skip to near
        # the end — overshooting count*2 to account for per-user
        # filtering (subject filter is applied server-side, but
        # stream_info.state.messages is the total count across all
        # subjects).
        try:
            info = await js.stream_info(self._wtmp_stream)
        except NotFoundError:
            return []
        last_seq = info.state.last_seq
        if last_seq == 0:
            return []

        batch = max(count * 2, _FETCH_BATCH)
        start_seq = max(1, last_seq - batch + 1)
        consumer_config = ConsumerConfig(
            deliver_policy=DeliverPolicy.BY_START_SEQUENCE,
            opt_start_seq=start_seq,
            inactive_threshold=_CONSUMER_INACTIVE_THRESHOLD,
        )

        # Use a unique named consumer per call and delete after use.
        # Ephemeral consumers (durable=None) leak when the connection
        # stays open — the same lesson documented in _durable_name().
        # A UUID suffix avoids collisions between concurrent sessions
        # of the same user (who share the same relay name).
        # inactive_threshold is the safety net for crash cleanup.
        consumer_name = f"{self._repo_name}-wtmp-peek-{uuid4().hex[:12]}"

        sub = await js.pull_subscribe(
            subject,
            durable=consumer_name,
            stream=self._wtmp_stream,
            config=consumer_config,
        )
        try:
            raw_msgs = await sub.fetch(batch=batch, timeout=_FETCH_TIMEOUT)
        except TimeoutError:
            raw_msgs = []
        finally:
            await sub.unsubscribe()
            with suppress(NotFoundError):
                await js.delete_consumer(self._wtmp_stream, consumer_name)

        events: list[SessionEvent] = []
        for raw in raw_msgs:
            try:
                event = SessionEvent.model_validate_json(raw.data)
            except (ValidationError, ValueError):
                logger.warning("Skipping malformed wtmp message")
            else:
                if event.version == 1:
                    events.append(event)
                else:
                    logger.debug("Skipping wtmp v%d (unsupported)", event.version)
            await raw.ack()

        # Sort by timestamp descending (most recent first), take count
        events.sort(key=lambda e: e.timestamp, reverse=True)
        return events[:count]

    # -- Wall (team broadcast) --

    async def set_wall(self, wall: WallPost | None) -> None:
        """Set or clear the team wall in the sessions KV bucket."""
        _, kv = await self._ensure_connected()
        if wall is None:
            with suppress(KeyNotFoundError, BucketNotFoundError):
                await kv.delete(self._wall_kv_key)
        else:
            await kv.put(self._wall_kv_key, wall.model_dump_json().encode())

    async def get_wall(self, *, repo: str | None = None) -> WallPost | None:
        """Read the active wall, returning ``None`` if absent or expired.

        When *repo* is given, reads that repo's wall instead of this
        instance's own wall (cross-repo wall check, DES-030).
        """
        _, kv = await self._ensure_connected()
        key = self.wall_kv_key(repo) if repo else self._wall_kv_key
        try:
            entry = await kv.get(key)
            if entry.value is None:
                return None
            wall = WallPost.model_validate_json(entry.value)
        except (KeyNotFoundError, BucketNotFoundError, ValidationError, ValueError):
            return None
        if wall.is_expired:
            with suppress(KeyNotFoundError, BucketNotFoundError):
                await kv.delete(key)
            return None
        return wall

    async def set_wall_for_repo(self, repo: str, wall: WallPost | None) -> None:
        """Set or clear a wall for a specific repo (cross-repo broadcast)."""
        _, kv = await self._ensure_connected()
        key = self.wall_kv_key(repo)
        if wall is None:
            with suppress(KeyNotFoundError, BucketNotFoundError):
                await kv.delete(key)
        else:
            await kv.put(key, wall.model_dump_json().encode())
