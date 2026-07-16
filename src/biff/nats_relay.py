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
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
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
    KeyWrongLastSequenceError,
    NotFoundError,
)
from pydantic import ValidationError

from biff._stdlib import repo_org
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
_CONNECT_PROVISION_TIMEOUT = 20.0  # bound JetStream/KV provisioning so a
# disconnected connection can't hold _connect_lock forever and wedge every
# relay caller (biff-wr3)

# Keepalive tuning so a half-open connection (socket up, server not
# responding) is detected in ~60-80s, not the nats-py default of 240s
# (biff-tww).  Detection latency is roughly ``_PING_INTERVAL *
# _MAX_OUTSTANDING_PINGS`` (20*3 = ~60s), and up to one interval higher
# (~80s) because nats-py trips only when the outstanding-PING count
# *exceeds* the max.  nats-py defaults are
# ping_interval=120s / max_outstanding_pings=2 → ~240s, during which every
# JetStream/KV request times out and the poller + heartbeat crash-loop on
# ``nats: timeout`` with no recovery.  Prompt ping detection fires nats-py's
# own reconnect, which invalidates cached handles (DES-029) and rebuilds them.
_PING_INTERVAL = 20  # seconds between client→server PINGs
_MAX_OUTSTANDING_PINGS = 3  # unanswered PINGs before the conn is declared dead
# Never give up reconnecting: a bounded attempt count (nats-py default 60)
# lets a prolonged outage strand the MCP server permanently.  Infinite
# reconnect keeps the persistent connection (DES-019) alive across any outage;
# ``_on_reconnect`` + handle invalidation restore service when the server
# returns.  ``_on_closed`` still fires on explicit close (disconnect/close).
_MAX_RECONNECT_ATTEMPTS = -1  # infinite
_RECONNECT_TIME_WAIT = 2  # seconds between reconnect attempts (nats-py default)

# Proactive wedge detection (biff-3hp).  A half-open connection (socket up,
# server unresponsive) makes every JS/KV request raise ``nats: timeout``.
# The keepalive path (DES-041) recovers in ~60-80s; the proactive detector
# beats it by tearing the connection down after this many *consecutive*
# runtime timeouts on a still-connected socket, so the next
# ``_ensure_connected`` dials a fresh client.  Each timed-out request blocks
# for the nats-py JetStream request timeout (5s), so the detection bound is
# ``_WEDGE_FORCE_RECONNECT_THRESHOLD * 5s`` = ~15s — 4-5x faster than
# keepalive.  Three consecutive failures (not one) are required: a single slow
# request is one timeout that a following success clears, and three independent
# failed round-trips (~15s of sustained unresponsiveness) cannot be a transient
# blip.  This mirrors the keepalive's own "3 missed PINGs" wedge criterion.
_WEDGE_FORCE_RECONNECT_THRESHOLD = 3

# Default stream prefix (DES-016).  Tests override via stream_prefix="biff-dev".
_DEFAULT_STREAM_PREFIX = "biff"

# KV key namespaces reserved for encryption (DES-016, biff-lff).
# Session keys are {repo}.{user}.{tty}; these prefixes are not sessions.
# Name reservations live in a separate ``biff-names`` bucket, so "name"
# does not belong here — it would silently block a user named "name".
RESERVED_KV_NAMESPACES: frozenset[str] = frozenset({"key"})


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


class _ConnectionHealth:
    """Single source of connection-health diagnostics for :class:`NatsRelay`.

    Tracks connection lifetime, recovery latency, and the consecutive
    runtime-timeout count that signals a half-open connection (socket up,
    server unresponsive).  Every connection-cluster log line is emitted
    here so onset and recovery are logged once, not once per poller tick
    (DES logging standard: log at the decision point, not every layer).

    Timings use ``time.monotonic()`` — immune to wall-clock adjustments.
    The consecutive-timeout count is exposed via :attr:`consecutive_timeouts`,
    and :meth:`should_force_reconnect` latches the once-per-episode decision to
    force a proactive reconnect (biff-3hp).  This class only *decides*; it never
    changes connection state — :class:`NatsRelay` owns the teardown.
    """

    def __init__(self, url: str) -> None:
        self._host = self._host_of(url)
        self._connected_at: float | None = None
        self._disconnected_at: float | None = None
        self._last_ok: float | None = None
        self._timeout_count = 0
        self._wedge_onset_at: float | None = None
        self._force_reconnect_fired = False

    @staticmethod
    def _host_of(url: str) -> str:
        """Return ``host[:port]`` from the first server in *url*, dropping creds.

        A NATS URL may embed ``user:pass@`` and may be a comma-separated
        cluster list.  Never log userinfo (PII); unparseable input yields a
        placeholder rather than echoing the raw (credentialed) URL.
        """
        first = url.split(",", 1)[0].strip()
        if "//" not in first:  # urlsplit needs a // authority to find creds/host
            first = "//" + first
        try:
            parts = urlsplit(first)
            host, port = parts.hostname, parts.port
        except ValueError:
            return "<unknown host>"
        if host is None:
            return "<unknown host>"
        if ":" in host:  # IPv6 — bracket regardless of port
            host = f"[{host}]"
        return f"{host}:{port}" if port is not None else host

    @property
    def consecutive_timeouts(self) -> int:
        """Runtime JS/KV timeouts since the last success (biff-3hp foundation)."""
        return self._timeout_count

    def record_connected(self, provision_ms: float, *, is_new_connection: bool) -> None:
        """Record a successful connect + provision.

        Logs INFO only for a freshly dialed connection.  A re-provision on
        an already-reconnected client updates timing state silently — the
        reconnect INFO from :meth:`record_reconnected` already covered it.
        """
        now = time.monotonic()
        self._connected_at = now
        self._last_ok = now
        self._disconnected_at = None
        self._timeout_count = 0
        self._wedge_onset_at = None
        self._force_reconnect_fired = False
        if is_new_connection:
            logger.info(
                "Connected to NATS at %s (JS/KV provisioned in %.0fms)",
                self._host,
                provision_ms,
            )

    def record_provision_timeout(self, seconds: float) -> None:
        """Log an INFO when JetStream/KV provisioning exceeds its bound.

        INFO, not WARNING: provisioning timeout tears the connection down and
        the next call reconnects — a transient, self-recovering event.  At
        WARNING it would clear the CLI's stderr floor and print into the
        interactive REPL; at INFO it reaches biff.log only (biff-9la).
        """
        logger.info(
            "NATS provisioning timed out after %.0fs at %s — tearing down connection",
            seconds,
            self._host,
        )

    def record_disconnected(self) -> None:
        """Log an INFO with connection lifetime — the frequency signal.

        Short lifetimes recurring in the log mean the connection keeps
        flapping; a long lifetime before a disconnect is a one-off outage.

        INFO, not WARNING: a disconnect is a transient, auto-recovering event
        (nats-py reconnects) — it must stay off the CLI's WARNING stderr floor
        and reach biff.log only, or it prints into the interactive REPL
        (biff-9la).

        Clears the wedge counter and force-reconnect latch: once nats-py's
        keepalive has declared the socket down, it owns recovery, so the
        proactive teardown (biff-3hp) must not also fire.
        """
        self._timeout_count = 0
        self._wedge_onset_at = None
        self._force_reconnect_fired = False
        self._disconnected_at = time.monotonic()
        if self._connected_at is not None:
            uptime = self._disconnected_at - self._connected_at
            logger.info(
                "Disconnected from NATS at %s after %.0fs connected",
                self._host,
                uptime,
            )
        else:
            logger.info("Disconnected from NATS at %s", self._host)
        self._connected_at = None

    def record_reconnected(self) -> None:
        """Log an INFO with downtime since disconnect — the recovery latency.

        Resets the wedge counter: the reconnect is itself the recovery
        signal, so a later first-success must not also log a recovery line.
        """
        now = time.monotonic()
        if self._disconnected_at is not None:
            downtime = now - self._disconnected_at
            logger.info(
                "Reconnected to NATS at %s after %.0fs down", self._host, downtime
            )
        else:
            logger.info("Reconnected to NATS at %s", self._host)
        self._connected_at = now
        self._last_ok = now
        self._timeout_count = 0
        self._wedge_onset_at = None
        self._force_reconnect_fired = False

    def record_closed(self) -> None:
        """Log an INFO when the connection closes for good.

        INFO, not WARNING: a close fires on intentional teardown (CLI exit,
        disconnect) and on nats-py giving up mid-reconnect — background
        lifecycle events that must not clear the CLI's WARNING stderr floor and
        print into the interactive REPL; biff.log keeps them (biff-9la).

        Clears the wedge counter and force-reconnect latch: a close is a
        lifecycle reset point like connect/disconnect/reconnect, so the latch
        must not outlive it regardless of handle state (defense-in-depth —
        the next connect resets too, but the latch's safety should not depend
        on a different callback nulling the handles).
        """
        logger.info("NATS connection closed at %s", self._host)
        self._connected_at = None
        self._timeout_count = 0
        self._wedge_onset_at = None
        self._force_reconnect_fired = False

    def record_success(self) -> None:
        """Record a successful runtime JS/KV request.

        Logs a single recovery INFO when the connection had been timing
        out (a half-open period that cleared before nats-py's ping loop
        tripped a full reconnect).
        """
        now = time.monotonic()
        if self._timeout_count > 0:
            onset = self._wedge_onset_at
            over = now - onset if onset is not None else 0.0
            logger.info(
                "NATS recovered after %d timeouts over %.0fs",
                self._timeout_count,
                over,
            )
            self._timeout_count = 0
            self._wedge_onset_at = None
        self._force_reconnect_fired = False
        self._last_ok = now

    def should_force_reconnect(self, threshold: int) -> bool:
        """Return ``True`` exactly once when timeouts reach *threshold*.

        The latch fires the proactive teardown once per wedge episode, not on
        every timeout past the threshold.  It is cleared whenever the counter
        resets (success, connect, reconnect, disconnect, close), so a later
        episode re-arms it.
        """
        if self._force_reconnect_fired or self._timeout_count < threshold:
            return False
        self._force_reconnect_fired = True
        return True

    def record_timeout(self, operation: str, *, is_connected: bool) -> None:
        """Record a runtime JS/KV timeout, logging the onset once at INFO.

        The first timeout after a healthy period is the wedge onset — it
        describes the connection state and staleness in prose so the log is
        self-explaining.  Repeats are counted silently to avoid loop-spam.

        INFO, not WARNING: a half-open timeout self-recovers (keepalive or the
        proactive teardown reconnects), so it must stay off the CLI's WARNING
        stderr floor and reach biff.log only — otherwise the onset prints into
        the interactive REPL (biff-9la).

        The wording tracks the actual state: a still-connected socket that
        stops answering is half-open; a socket that is mid-reconnect is
        described as such, so the line never overstates the diagnosis.
        """
        if self._timeout_count == 0:
            self._wedge_onset_at = time.monotonic()
            if is_connected:
                state = (
                    "connection appears half-open — socket still open but the "
                    "server is not responding"
                )
            else:
                state = "connection is not responding — socket is reconnecting"
            logger.info(
                "NATS request timed out (%s); %s, last successful request %s ago",
                operation,
                state,
                self._seconds_since_ok(),
            )
        self._timeout_count += 1

    def _seconds_since_ok(self) -> str:
        """Return a human phrase for staleness since the last good request."""
        if self._last_ok is None:
            return "an unknown time"
        return f"{time.monotonic() - self._last_ok:.0f}s"


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
        # Talk routes on (org, identity), never on repo (talk.tex subjectOf):
        # the org is the isolation boundary, derived once from our own repo.
        self._org = repo_org(repo_name)
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
        self._names_bucket = f"{stream_prefix}-names"
        self._nc: NatsClient | None = None
        self._js: JetStreamContext | None = None
        self._kv: KeyValue | None = None
        self._names_kv: KeyValue | None = None
        self._connect_lock = asyncio.Lock()
        self._wtmp_available: bool = False
        self._health = _ConnectionHealth(url)
        # Monotonic per-dial token.  Each new client dialed in
        # ``_open_connection`` bumps it; the connection callbacks capture the
        # value in force at their registration and no-op when it no longer
        # matches, so a superseded client's late callback cannot mutate state
        # for the live connection (Bugbot: stale-callback race).
        self._generation = 0

    def _auth_kwargs(self) -> dict[str, str]:
        """Build authentication keyword arguments for ``nats.connect()``."""
        if self._auth is None:
            return {}
        return self._auth.as_nats_kwargs()

    def _cached_handles(
        self,
    ) -> tuple[JetStreamContext, KeyValue] | None:
        """Return cached handles if connection is alive, else None."""
        js, kv, nc, names_kv = self._js, self._kv, self._nc, self._names_kv
        if (
            js is not None
            and kv is not None
            and names_kv is not None
            and nc is not None
            and not nc.is_closed
        ):
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

    async def _tracked[T](self, operation: str, awaitable: Awaitable[T]) -> T:
        """Await a runtime JS/KV request, feeding connection-health diagnostics.

        On ``nats: timeout`` the wedge onset is recorded once (with transport
        state) before re-raising, so callers see identical behavior.  On
        success any active wedge is cleared with a single recovery line.
        This is the choke point every hot-loop relay method routes its
        primary request through, so onset/recovery is logged once — never
        once per poller tick.

        Health accounting is charged to the client that *owned* the request,
        captured before the await.  If keepalive declares that client down and
        ``_ensure_connected`` dials a fresh one while the request is pending,
        ``self._nc`` no longer matches ``owner`` when the request settles.  A
        timeout or success on a superseded client is then a no-op for the live
        connection — it must never force-reconnect a healthy client nor clear
        the live client's wedge latch (Copilot: tracked-timeout race).
        """
        # INVARIANT: callers fetch the handle via ``_ensure_connected()``
        # immediately before each ``_tracked``, with no ``await`` in between —
        # so ``owner = self._nc`` at entry is the connection the awaitable runs
        # on.  ``owner`` is the attribution token for the request; an ``await``
        # in that gap could let a concurrent rebuild swap ``self._nc`` while the
        # awaitable still runs on the stale handle, so a timeout on that handle
        # would be misattributed to the fresh client and could spuriously
        # force-reconnect it.  Sites issuing two sequential ``_tracked`` calls
        # (``get_unread_summary``, ``heartbeat``) re-fetch the handle before the
        # second one to preserve this.
        owner = self._nc
        try:
            result = await awaitable
        except TimeoutError:
            if self._nc is owner:
                is_connected = owner is not None and owner.is_connected
                self._health.record_timeout(operation, is_connected=is_connected)
                # Proactive wedge recovery (biff-3hp): only on a still-connected
                # socket — if nats-py's keepalive already declared the connection
                # down (is_connected False), it owns the reconnect.
                if is_connected and self._health.should_force_reconnect(
                    _WEDGE_FORCE_RECONNECT_THRESHOLD
                ):
                    await self._force_reconnect()
            raise
        except (KeyNotFoundError, BucketNotFoundError, NotFoundError):
            # A "not found" is the server answering — proof of liveness.
            # Record success so last_ok/wedge counters stay accurate, then
            # re-raise so callers see identical behavior.  Do NOT treat other
            # errors (NoRespondersError, connection faults) as success.
            if self._nc is owner:
                self._health.record_success()
            raise
        if self._nc is owner:
            self._health.record_success()
        return result

    async def _force_reconnect(self) -> None:
        """Tear down a half-open connection so the next call rebuilds it fresh.

        Fired by :meth:`_tracked` after ``_WEDGE_FORCE_RECONNECT_THRESHOLD``
        consecutive runtime timeouts on a still-connected socket — the
        half-open signature (DES-041, biff-3hp).  Rather than wait out the
        ~60-80s keepalive floor, close the socket and clear the cached handles;
        the next :meth:`_ensure_connected` dials a fresh client.

        Serialised on ``_connect_lock`` against concurrent rebuilds
        (``_ensure_connected`` / ``_open_connection`` hold it).  ``_tracked``
        requests never run under that lock, so acquiring it here cannot
        deadlock.  ``close()`` and ``disconnect()`` do *not* take the lock, so
        races with deliberate teardown are handled by idempotence, not by the
        lock: the wedged client is captured before the lock and re-checked
        under it, and if it no longer matches ``self._nc`` (a rebuild, close,
        or disconnect replaced or cleared it) this is a no-op — it never tears
        down a freshly built connection.  The re-check also skips a client that
        stopped being connected while we waited for the lock: if nats-py's own
        keepalive flipped it to reconnecting after the ``_tracked`` gate saw it
        connected, that reconnect owns recovery — do not tear it down.
        """
        wedged = self._nc
        if wedged is None or wedged.is_closed:
            return
        async with self._connect_lock:
            if self._nc is not wedged or wedged.is_closed or not wedged.is_connected:
                return
            logger.info(
                "NATS wedge confirmed after %d timed-out requests — forcing reconnect",
                _WEDGE_FORCE_RECONNECT_THRESHOLD,
            )
            await safe_close(wedged)
            self._nc = None
            self._js = None
            self._kv = None
            self._names_kv = None

    def _connection_callbacks(
        self, generation: int
    ) -> dict[str, Callable[..., Awaitable[None]]]:
        """Return the nats-py lifecycle callbacks bound to *generation*.

        A callback mutates connection state only while ``self._generation``
        still equals *generation*.  When ``_force_reconnect`` closes a wedged
        client and the next dial stores a fresh one, the old client's callbacks
        keep firing asynchronously; the generation check makes each a no-op so
        a superseded client can never null ``self._nc`` or the handles of the
        live connection (Bugbot: stale-callback race).
        """

        async def _on_disconnect() -> None:
            if self._generation != generation:
                return  # stale callback from a superseded client
            # Health tracker owns the log line (with connection lifetime).
            self._health.record_disconnected()
            # Proactively invalidate cached handles so the next tool call
            # reconnects instead of using stale JetStream/KV refs (DES-029).
            self._js = None
            self._kv = None
            self._names_kv = None

        async def _on_reconnect() -> None:
            if self._generation != generation:
                return  # stale callback from a superseded client
            # Health tracker owns the log line (with recovery latency).
            self._health.record_reconnected()

        async def _on_closed() -> None:
            if self._generation != generation:
                return  # stale callback from a superseded client
            # nats-py gave up reconnecting (or the connection closed).  Drop
            # the client so the next _ensure_connected builds a fresh one
            # instead of reusing a dead connection (biff-wr3).
            self._health.record_closed()
            self._nc = None
            self._js = None
            self._kv = None
            self._names_kv = None

        async def _on_error(exc: Exception) -> None:
            if self._generation != generation:
                return  # stale callback from a superseded client
            # Python 3.14 raises APPLICATION_DATA_AFTER_CLOSE_NOTIFY during TLS
            # teardown.  Harmless — suppress.
            ssl_teardown = isinstance(exc, ssl.SSLError) and (
                "APPLICATION_DATA_AFTER_CLOSE_NOTIFY" in str(exc)
            )
            if not ssl_teardown:
                # INFO, not ERROR: these callback errors (TimeoutError, SSL
                # shutdown timed out, Disconnected) are transient, background
                # events that nats-py auto-recovers from — the connection
                # self-heals without a restart.  At ERROR they cleared the
                # CLI's WARNING stderr floor and dumped a traceback into the
                # interactive REPL (biff-9la); at INFO they reach biff.log
                # only.  exc_info keeps the full traceback for diagnosis; %r
                # keeps the cause visible for message-less errors (biff-wr3).
                logger.info(
                    "NATS error (transient, auto-recovering): %r", exc, exc_info=exc
                )

        return {
            "disconnected_cb": _on_disconnect,
            "reconnected_cb": _on_reconnect,
            "closed_cb": _on_closed,
            "error_cb": _on_error,
        }

    async def _open_connection(self) -> tuple[JetStreamContext, KeyValue]:
        """Create a new NATS connection and provision infrastructure.

        Must be called while holding ``_connect_lock``.
        """
        nc = self._nc
        is_new_connection = False
        if nc is None or nc.is_closed:
            is_new_connection = True
            # Capture this dial's generation.  A callback belongs to the
            # current client only while ``self._generation`` still equals the
            # value it closed over; a superseded client's late callback finds a
            # newer generation and no-ops (Bugbot: stale-callback race).
            self._generation += 1
            nc = await nats.connect(  # pyright: ignore[reportUnknownMemberType]
                self._url,
                name=self._name,
                ping_interval=_PING_INTERVAL,
                max_outstanding_pings=_MAX_OUTSTANDING_PINGS,
                max_reconnect_attempts=_MAX_RECONNECT_ATTEMPTS,
                reconnect_time_wait=_RECONNECT_TIME_WAIT,
                **self._connection_callbacks(self._generation),
                **self._auth_kwargs(),
            )

        provision_start = time.monotonic()
        try:
            # Bound provisioning: a disconnected/reconnecting connection
            # (is_closed is False, is_connected is False) lets JetStream/KV
            # calls block forever while we hold _connect_lock, wedging every
            # relay caller (biff-wr3).  Time it out so the lock is always
            # released and callers get an error instead of hanging.
            js, kv, names_kv = await asyncio.wait_for(
                self._provision(nc), timeout=_CONNECT_PROVISION_TIMEOUT
            )
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                self._health.record_provision_timeout(_CONNECT_PROVISION_TIMEOUT)
            # Tear the connection down so the next call reconnects fresh
            # instead of reusing a wedged/half-open one.
            await safe_close(nc)
            self._nc = None
            self._js = None
            self._kv = None
            self._names_kv = None
            raise

        self._nc = nc
        self._js = js
        self._kv = kv
        self._names_kv = names_kv
        provision_ms = (time.monotonic() - provision_start) * 1000
        self._health.record_connected(provision_ms, is_new_connection=is_new_connection)
        return js, kv

    async def _provision(
        self, nc: NatsClient
    ) -> tuple[JetStreamContext, KeyValue, KeyValue]:
        """Provision JetStream, the KV buckets, and the inbox stream on *nc*.

        Run under a timeout by :meth:`_open_connection` so a disconnected
        connection cannot block relay callers indefinitely (biff-wr3).
        Returns ``(js, sessions_kv, names_kv)``.
        """
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

        # KV bucket for TTY name reservations — shared across all repos (DES-035).
        # Separate from sessions: no repo prefix in keys, 1 MiB max.
        names_config = KeyValueConfig(
            bucket=self._names_bucket,
            ttl=_KV_TTL,
            max_bytes=1 * 1024 * 1024,  # 1 MiB
        )
        try:
            names_kv = await js.create_key_value(  # pyright: ignore[reportUnknownMemberType]
                config=names_config,
            )
        except BadRequestError:
            logger.info(
                "Shared names KV bucket %s config differs, using as-is",
                self._names_bucket,
            )
            names_kv = await js.key_value(self._names_bucket)  # pyright: ignore[reportUnknownMemberType]

        await self._provision_wtmp(js)
        await self._cleanup_legacy_streams(js)
        return js, kv, names_kv

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
                # INFO: wtmp degrades gracefully (session history disabled),
                # core messaging is unaffected — a background provisioning
                # event that must stay off the CLI terminal (biff-9la).
                logger.info("Wtmp stream unavailable: %s", exc)
                self._wtmp_available = False
            else:
                # Shared stream config differs — use as-is.
                logger.info(
                    "Shared wtmp stream %s config differs, using as-is",
                    self._wtmp_stream,
                )
                self._wtmp_available = True
        except Exception:  # noqa: BLE001 — provisioning must never crash startup
            # INFO: degrades gracefully; a background connect/reconnect event
            # that must not print into the interactive REPL (biff-9la).  The
            # traceback stays in biff.log for diagnosis.
            logger.info("Wtmp stream provisioning failed", exc_info=True)
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
            # INFO: best-effort background cleanup on connect/reconnect; a
            # failure is non-fatal and must not print into the interactive
            # REPL (biff-9la).  The traceback stays in biff.log.
            logger.info("Legacy stream cleanup failed", exc_info=True)

    @property
    def wtmp_available(self) -> bool:
        """Whether the wtmp stream was successfully provisioned."""
        return self._wtmp_available

    @property
    def connection_generation(self) -> int:
        """Monotonic per-dial token — bumps once per *new* client.

        Advances only when :meth:`_open_connection` dials a fresh
        ``nats.connect`` (a client replacement after ``_force_reconnect``
        or ``_on_closed``); nats-py's in-place keepalive reconnect reuses
        the same client and leaves it unchanged.  Core-NATS subscribers
        (the always-on talk SUB) compare against this to tell a client
        replacement — which orphans their subscription on the closed
        client and must be re-established — from a transparent reconnect,
        which replays every SUB (``nats-relay.tex`` ``talkSubGen``).
        """
        return self._generation

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
        self._names_kv = None
        self._wtmp_available = False

    async def purge_data(self) -> None:
        """Purge this repo's data from shared streams without deleting infrastructure.

        Subject-filtered purge (DES-016): only removes KV keys and
        messages belonging to this repo, leaving other repos' data
        intact.  Keeps the shared bucket and streams themselves.

        Purges the underlying KV stream (``KV_{bucket}``) directly
        instead of calling ``kv.keys()`` which creates an ephemeral
        consumer via ``kv.watch()`` — those leak on long-lived
        connections.
        """
        js, _ = await self._ensure_connected()
        kv_stream = f"KV_{self._kv_bucket}"
        kv_subject = f"$KV.{self._kv_bucket}.{self._repo_name}.>"
        with suppress(NotFoundError):
            await js.purge_stream(kv_stream, subject=kv_subject)  # pyright: ignore[reportUnknownMemberType]
        # Name reservations (biff-names) are global, not repo-scoped —
        # purge_data must not touch them. Names expire via KV TTL.
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
        self._names_kv = None

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
        self._names_kv = None

    async def close(self) -> None:
        """Close the NATS connection and release resources."""
        if self._nc is not None:
            await safe_close(self._nc)
            self._nc = None
            self._js = None
            self._kv = None
            self._names_kv = None

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
    def _validate_repo(repo: str) -> str:
        """Reject repo names that could escape NATS subject boundaries."""
        if not repo or any(c in repo for c in (".", "*", ">", " ")):
            msg = f"Invalid repo name: {repo!r}"
            raise ValueError(msg)
        return repo

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
        repo = self._validate_repo(target_repo) if target_repo else self._repo_name
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
        """KV key for a session: ``{repo}.{user}.{tty}`` (DES-016)."""
        user, tty = session_key.split(":", maxsplit=1)
        self._validate_user(user)
        self._validate_tty(tty)
        return f"{self._repo_name}.{user}.{tty}"

    @staticmethod
    def wall_kv_key(repo_name: str) -> str:
        """KV key for the team wall: ``{repo}.wall`` (DES-016)."""
        return f"{repo_name}.wall"

    @property
    def _wall_kv_key(self) -> str:
        """Instance shorthand for :meth:`wall_kv_key`."""
        return self.wall_kv_key(self._repo_name)

    def talk_notify_subject(self, session_key: str) -> str:
        """NATS core subject for talk frames addressed to *session_key*.

        Keyed on ``(org, identity)`` (talk.tex ``subjectOf``): the
        organization is the isolation boundary and the globally-unique
        ``user:tty`` identity selects the one session, so a frame reaches
        the addressed ``@user:tty`` whatever repository either party runs
        in.  The repository is never a routing coordinate — a reply routes
        to ``(myOrg, peer)`` from the sender's own org and the peer's
        identity, both held locally (biff-e9u).  A session subscribes to
        ``subjectOf`` of its own key; a sender publishes to ``subjectOf``
        of the peer's key.  Core NATS (no stream) — frames are dropped if
        nobody is listening.
        """
        user, _, tty = session_key.partition(":")
        self._validate_user(user)
        self._validate_tty(tty)
        return f"{self._stream_prefix}.{self._org}.talk.notify.{user}:{tty}"

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
            await self._tracked(
                "publish", js.publish(subject, message.model_dump_json().encode())
            )
        else:
            # Broadcast — single user subject, no session lookup
            self._validate_user(message.to_user)
            if target_repo:
                repo = self._validate_repo(target_repo)
                prefix = f"{self._stream_prefix}.{repo}.inbox"
                subject = f"{prefix}.{message.to_user}"
            else:
                subject = self._user_subject(message.to_user)
            await self._tracked(
                "publish", js.publish(subject, message.model_dump_json().encode())
            )

        # Notify any active talk_listen subscriber (core NATS, fire-and-forget).
        await self._publish_talk_notification(message.to_user, message, sender_key)

    async def _publish_talk_notification(
        self,
        to_user: str,
        message: Message | None = None,
        sender_key: str = "",
    ) -> None:
        """Publish a talk notification so ``talk_listen`` wakes up.

        The payload carries the sender, message body, and sender session
        key so the status line poller can display incoming talk messages
        and reject self-echo.  Falls back to ``b"1"`` if no message.

        The subject is ``subjectOf`` of the targeted recipient identity
        (talk.tex): only a ``user:tty`` recipient names a single session to
        wake.  A bare-user broadcast has no session identity, so no
        instant-wake frame is published — its recipient still drains the
        durable inbox on the next poll tick.

        Best-effort: failures are logged at debug level and never
        propagate — the JetStream delivery (the critical path) has
        already succeeded.
        """
        if self._nc is None or self._nc.is_closed:
            return
        if ":" not in to_user:
            return
        try:
            subject = self.talk_notify_subject(to_user)
            if message is not None:
                data: dict[str, str] = {
                    "from": message.from_user,
                    "body": message.body,
                    "to_key": to_user,
                }
                if sender_key:
                    data["from_key"] = sender_key
                if message.from_tty:
                    data["from_tty"] = message.from_tty
                payload = json.dumps(data).encode()
            else:
                payload = b"1"
            await self._nc.publish(subject, payload)
        except Exception:  # noqa: BLE001 — notification is best-effort
            logger.debug("Talk notification failed for %s", to_user)

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
            info = await self._tracked(
                "stream_info",
                js.stream_info(self._stream_name, subjects_filter=subject),
            )
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
            tty_info = await self._tracked(
                "stream_info",
                js.stream_info(self._stream_name, subjects_filter=tty_subject),
            )
            if tty_info.state.subjects:
                tty_count = tty_info.state.subjects.get(tty_subject, 0)
        except NotFoundError:
            pass
        # Re-anchor to the current connection: the first _tracked's await
        # may have let a concurrent loop rebuild self._nc, leaving `js`
        # stale.  Fast path returns cached handles, so `owner = self._nc` at
        # _tracked entry matches the connection this awaitable runs on
        # (code-reviewer + alex-chen: residual two-_tracked race).  Kept OUT of
        # the try below so a slow-path rebuild raising BucketNotFoundError (a
        # NotFoundError subclass) surfaces as a provisioning failure instead of
        # being swallowed into a tty_count-only undercount (silent-failure-hunter).
        js, _ = await self._ensure_connected()
        try:
            user_info = await self._tracked(
                "stream_info",
                js.stream_info(self._stream_name, subjects_filter=user_subject),
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
        await self._tracked(
            "kv.put", kv.put(kv_key, session.model_dump_json().encode())
        )

    async def get_session(self, session_key: str) -> UserSession | None:
        """Read a single session from KV by ``{user}:{tty}`` key."""
        kv_key = self._kv_key(session_key)
        _, kv = await self._ensure_connected()
        try:
            entry = await self._tracked("kv.get", kv.get(kv_key))
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
            entry = await self._tracked("kv.get", kv.get(kv_key))
            if entry.value is None:
                return  # No session to heartbeat
            existing = UserSession.model_validate_json(entry.value)
        except (KeyNotFoundError, BucketNotFoundError):
            return  # Session not found — nothing to heartbeat
        except (ValidationError, ValueError):
            # INFO: heartbeat runs in the background loop; a skipped tick is
            # harmless (3-day TTL) and must not print into the interactive
            # REPL (biff-9la).  biff.log records the anomaly.
            logger.info("Corrupt session for %s, skip heartbeat", session_key)
            return
        updated = existing.model_copy(update={"last_active": datetime.now(UTC)})
        # Re-anchor to the current connection: the kv.get above may have let a
        # concurrent loop rebuild self._nc, leaving `kv` stale.  Fast path
        # returns cached handles, so `owner = self._nc` at _tracked entry
        # matches the connection this awaitable runs on — and the put runs on a
        # live handle (code-reviewer + alex-chen: residual two-_tracked race).
        _, kv = await self._ensure_connected()
        await self._tracked(
            "kv.put", kv.put(kv_key, updated.model_dump_json().encode())
        )
        # Refresh TTY name reservation to prevent TTL expiry (DES-035).
        if existing.tty_name:
            try:
                await self.refresh_tty_reservation(
                    existing.user, existing.tty_name, session_key
                )
            except Exception:  # noqa: BLE001
                # INFO: reservation refresh runs inside the background
                # heartbeat; a transient failure retries next tick and must
                # not print into the interactive REPL (biff-9la).
                logger.info("Failed to refresh TTY name reservation", exc_info=True)

    async def get_sessions(self) -> list[UserSession]:
        """Return all sessions for this repo (NATS KV TTL handles expiry).

        Delegates to :meth:`_get_sessions_for_repo` with this relay's
        own repo name.
        """
        return await self._get_sessions_for_repo(self._repo_name)

    async def get_sessions_for_repos(self, repos: frozenset[str]) -> list[UserSession]:
        """Return sessions from multiple repos via parallel per-repo queries.

        Each repo gets its own ``stream_info`` call with a repo-scoped
        ``subjects_filter`` (``$KV.{bucket}.{repo}.>``), keeping all
        filtering server-side.  Results are merged and returned as a
        single list.  Used by cross-repo commands (``/who``, ``/finger``)
        when peers are configured (DES-030).
        """
        if not repos:
            return []
        if len(repos) == 1:
            (repo,) = repos
            return await self._get_sessions_for_repo(repo)
        repo_results = await asyncio.gather(
            *(self._get_sessions_for_repo(repo) for repo in repos)
        )
        return [s for batch in repo_results for s in batch]

    async def discover_repos_for_org(self, org: str) -> frozenset[str]:
        """Discover repos with active sessions under an org prefix.

        Uses a single ``stream_info`` with ``subjects_filter`` scoped to
        the org's KV key prefix (``$KV.{bucket}.{org}__>``).  Returns
        repo names extracted from subject metadata — no session values
        are fetched (DES-034).

        Returns an empty frozenset on any error (transient NATS failures
        must not break startup).
        """
        try:
            return await self._discover_repos_for_org_inner(org)
        except NotFoundError:
            return frozenset()
        except Exception:  # noqa: BLE001
            # INFO: org discovery runs at session startup and is best-effort
            # (returns empty on any transient failure) — it must not print a
            # traceback into the interactive REPL (biff-9la).  biff.log keeps
            # the full detail.
            logger.info("Org discovery failed for %s", org, exc_info=True)
            return frozenset()

    async def _discover_repos_for_org_inner(self, org: str) -> frozenset[str]:
        """Inner implementation — may raise on NATS errors."""
        js, _ = await self._ensure_connected()
        kv_stream = f"KV_{self._kv_bucket}"
        kv_prefix = f"$KV.{self._kv_bucket}."
        # org = "punt-labs", repos are keyed as "punt-labs__biff.user.tty"
        # Filter: $KV.biff-sessions.punt-labs__> matches all repos under this org
        org_filter = f"{kv_prefix}{org}__>"
        info = await self._tracked(
            "stream_info",
            js.stream_info(kv_stream, subjects_filter=org_filter),
        )
        if not info.state.subjects:
            return frozenset()
        repos: set[str] = set()
        for subject in info.state.subjects:
            key = subject.removeprefix(kv_prefix)
            parts = key.split(".", maxsplit=2)
            if len(parts) < 2:
                continue
            repo = parts[0]
            if parts[1] in RESERVED_KV_NAMESPACES:
                continue
            repos.add(repo)
        return frozenset(repos)

    async def _get_sessions_for_repo(self, repo: str) -> list[UserSession]:
        """Return sessions for a single repo (server-side filtered).

        Catches all errors and returns an empty list on failure so that
        a transient error on one peer repo does not take down the entire
        ``get_sessions_for_repos`` call.
        """
        try:
            return await self._get_sessions_for_repo_inner(repo)
        except NotFoundError:
            return []
        except Exception:  # noqa: BLE001
            # INFO: a transient peer-repo query failure returns [] and
            # self-recovers on the next call — it must not print a traceback
            # into the interactive REPL (biff-9la).  biff.log keeps the detail.
            logger.info("Failed to query sessions for repo %s", repo, exc_info=True)
            return []

    async def _get_sessions_for_repo_inner(self, repo: str) -> list[UserSession]:
        """Inner implementation — may raise on NATS errors."""
        js, kv = await self._ensure_connected()
        kv_stream = f"KV_{self._kv_bucket}"
        kv_prefix = f"$KV.{self._kv_bucket}."
        repo_filter = f"{kv_prefix}{repo}.>"
        info = await self._tracked(
            "stream_info",
            js.stream_info(kv_stream, subjects_filter=repo_filter),
        )
        if not info.state.subjects:
            return []
        session_keys: list[str] = []
        for subject in info.state.subjects:
            key = subject.removeprefix(kv_prefix)
            parts = key.split(".", maxsplit=2)
            if len(parts) != 3 or parts[0] != repo:
                continue
            if parts[1] in RESERVED_KV_NAMESPACES:
                continue
            session_keys.append(key)

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
            # INFO: the consumer auto-expires (inactive_threshold), so this
            # teardown-path failure is self-healing and must not print into
            # the interactive REPL (biff-9la).
            logger.info(
                "Consumer cleanup failed for %s (will auto-expire): %s",
                session_key,
                exc,
            )

    # -- TTY name reservation (DES-035) --

    async def _ensure_names_kv(self) -> KeyValue:
        """Return the names KV handle, connecting if necessary."""
        await self._ensure_connected()
        assert self._names_kv is not None  # noqa: S101 — guaranteed by _open_connection
        return self._names_kv

    async def reserve_tty_name(self, user: str, name: str, session_key: str) -> bool:
        """Atomically reserve a TTY name via NATS KV ``create()``.

        Returns ``True`` on success, ``False`` if the name is already taken.
        """
        self._validate_user(user)
        names_kv = await self._ensure_names_kv()
        key = f"{user}.{name}"
        try:
            await names_kv.create(key, session_key.encode())
            return True
        except KeyWrongLastSequenceError:
            return False

    async def release_tty_name(self, user: str, name: str) -> None:
        """Release a TTY name reservation."""
        self._validate_user(user)
        names_kv = await self._ensure_names_kv()
        key = f"{user}.{name}"
        with suppress(KeyNotFoundError, BucketNotFoundError):
            await names_kv.delete(key)

    async def refresh_tty_reservation(
        self, user: str, name: str, session_key: str
    ) -> None:
        """Refresh a TTY name reservation to prevent TTL expiry.

        Uses compare-and-set to avoid overwriting a reservation
        legitimately claimed by another session after TTL lapse.
        """
        # INFO throughout: refresh runs inside the background heartbeat loop,
        # and every branch here is a benign, self-recovering reservation race
        # (TTL lapse, another session took the name, concurrent update).  At
        # WARNING these would clear the CLI's stderr floor and print into the
        # interactive REPL (biff-9la); at INFO biff.log keeps the detail.
        self._validate_user(user)
        names_kv = await self._ensure_names_kv()
        key = f"{user}.{name}"
        try:
            entry = await names_kv.get(key)
        except (KeyNotFoundError, BucketNotFoundError):
            logger.info("TTY reservation %s gone, cannot refresh", key)
            return
        if entry.value is None or entry.value.decode() != session_key:
            logger.info(
                "TTY reservation %s owned by another session, skipping refresh",
                key,
            )
            return
        try:
            await names_kv.update(key, session_key.encode(), last=entry.revision)
        except KeyWrongLastSequenceError:
            logger.info(
                "TTY reservation %s changed concurrently, skipping refresh", key
            )

    async def get_tty_reservation_owner(self, user: str, name: str) -> str | None:
        """Return the session key that holds *name*, or ``None``."""
        self._validate_user(user)
        names_kv = await self._ensure_names_kv()
        key = f"{user}.{name}"
        try:
            entry = await names_kv.get(key)
            return entry.value.decode() if entry.value else None
        except (KeyNotFoundError, BucketNotFoundError):
            return None

    async def list_reserved_names(self, user: str) -> list[str]:
        """List reserved TTY names for a user via stream_info subject filter.

        Parses the tty_name from the KV subject
        ``$KV.{names_bucket}.{user}.{tty_name}``.
        """
        self._validate_user(user)
        js, _ = await self._ensure_connected()
        names_stream = f"KV_{self._names_bucket}"
        kv_prefix = f"$KV.{self._names_bucket}."
        user_filter = f"{kv_prefix}{user}.>"
        try:
            info = await js.stream_info(names_stream, subjects_filter=user_filter)
        except NotFoundError:
            return []
        if not info.state.subjects:
            return []
        names: list[str] = []
        for subject in info.state.subjects:
            key = subject.removeprefix(kv_prefix)
            parts = key.split(".", maxsplit=1)
            if len(parts) == 2 and parts[0] == user:
                names.append(parts[1])
        return names

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
        await self._tracked(
            "publish", js.publish(subject, event.model_dump_json().encode())
        )

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
            await self._tracked(
                "kv.put", kv.put(self._wall_kv_key, wall.model_dump_json().encode())
            )

    async def get_wall(self, *, repo: str | None = None) -> WallPost | None:
        """Read the active wall, returning ``None`` if absent or expired.

        When *repo* is given, reads that repo's wall instead of this
        instance's own wall (cross-repo wall check, DES-030).
        """
        _, kv = await self._ensure_connected()
        key = self.wall_kv_key(repo) if repo else self._wall_kv_key
        try:
            entry = await self._tracked("kv.get", kv.get(key))
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
