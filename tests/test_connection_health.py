"""Unit tests for NatsRelay connection-health diagnostic logging (biff-6px).

Covers the ``_ConnectionHealth`` single-source log points and the
``_tracked`` wedge onset/recovery choke point:

- A simulated half-open wedge (JS/KV raising ``nats: timeout``) logs the
  onset **once** and the recovery **once** — never one line per poller tick.
- connect / disconnect / reconnect carry the lifetime and downtime context
  that make a connection failure self-explaining in the log.
- No message body or plan text ever appears in a connection-cluster log
  line (PII guard).

These tests mock the connection, so they run in tiers 1-2 (no ``nats``
marker, no real server).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from nats.js.errors import KeyNotFoundError

from biff.models import Message, UserSession
from biff.nats_relay import (
    _WEDGE_FORCE_RECONNECT_THRESHOLD,
    NatsRelay,
    _ConnectionHealth,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_LOGGER_NAME = "biff.nats_relay"


class _Clock:
    """A settable stand-in for ``time.monotonic`` for deterministic timings."""

    def __init__(self) -> None:
        self._now = 0.0

    def set(self, value: float) -> None:
        self._now = value

    def __call__(self) -> float:
        return self._now


def _connected_nc() -> MagicMock:
    """A NATS client stand-in reporting a live, open connection."""
    nc = MagicMock()
    nc.is_closed = False
    nc.is_connected = True
    nc.close = AsyncMock()
    return nc


def _onset_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    # INFO, not WARNING: the wedge onset is a transient, self-recovering event
    # that must stay off the CLI's WARNING stderr floor (biff-9la).
    return [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "NATS request timed out" in r.getMessage()
    ]


def _recovery_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "recovered after" in r.getMessage()
    ]


class TestWedgeOnsetRecovery:
    """A half-open wedge logs onset once and recovery once — not per tick."""

    @pytest.mark.anyio()
    async def test_three_timeouts_log_one_onset_and_one_recovery(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        relay = NatsRelay(url="tls://fake:4222", repo_name="test")
        relay._nc = _connected_nc()
        relay._health.record_connected(5.0, is_new_connection=True)

        async def _timeout() -> str:
            raise TimeoutError

        async def _ok() -> str:
            return "ok"

        caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
        for _ in range(3):
            with pytest.raises(TimeoutError):
                await relay._tracked("stream_info", _timeout())
        result = await relay._tracked("stream_info", _ok())

        assert result == "ok"
        onsets = _onset_records(caplog)
        recoveries = _recovery_records(caplog)
        assert len(onsets) == 1, "wedge onset must log exactly once, not per tick"
        assert len(recoveries) == 1, "recovery must log exactly once"
        assert "3 timeouts" in recoveries[0].getMessage()

    @pytest.mark.anyio()
    async def test_onset_line_carries_transport_state(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        relay = NatsRelay(url="tls://fake:4222", repo_name="test")
        relay._nc = _connected_nc()
        relay._health.record_connected(5.0, is_new_connection=True)

        async def _timeout() -> str:
            raise TimeoutError

        caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
        with pytest.raises(TimeoutError):
            await relay._tracked("kv.get", _timeout())

        (onset,) = _onset_records(caplog)
        message = onset.getMessage()
        assert "kv.get" in message
        assert "half-open" in message
        assert "the server is not responding" in message
        assert "last successful request" in message
        # Pure prose — no field-name=value pairs (logging standard).
        assert "is_connected=" not in message
        assert "is_closed=" not in message

    def test_reconnecting_socket_is_not_called_half_open(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        health = _ConnectionHealth("tls://fake:4222")
        health.record_connected(5.0, is_new_connection=True)
        caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
        health.record_timeout("stream_info", is_connected=False)

        (onset,) = _onset_records(caplog)
        message = onset.getMessage()
        assert "half-open" not in message
        assert "reconnecting" in message

    def test_repeated_timeouts_are_counted_not_relogged(self) -> None:
        health = _ConnectionHealth("tls://fake:4222")
        health.record_connected(5.0, is_new_connection=True)
        for _ in range(5):
            health.record_timeout("stream_info", is_connected=True)
        assert health.consecutive_timeouts == 5
        health.record_success()
        assert health.consecutive_timeouts == 0

    @pytest.mark.anyio()
    async def test_not_found_response_is_liveness_proof(self) -> None:
        # A KeyNotFoundError is the server answering "no such key" — the
        # connection is healthy.  It must record success and clear the wedge,
        # not bypass tracking and let last_ok/counter go stale.
        relay = NatsRelay(url="tls://fake:4222", repo_name="test")
        relay._nc = _connected_nc()
        relay._health.record_connected(5.0, is_new_connection=True)

        async def _timeout() -> str:
            raise TimeoutError

        for _ in range(2):
            with pytest.raises(TimeoutError):
                await relay._tracked("kv.get", _timeout())
        assert relay._health.consecutive_timeouts == 2

        async def _not_found() -> str:
            raise KeyNotFoundError

        with pytest.raises(KeyNotFoundError):
            await relay._tracked("kv.get", _not_found())

        assert relay._health.consecutive_timeouts == 0  # wedge cleared
        assert relay._health._wedge_onset_at is None  # no lingering onset

    @pytest.mark.anyio()
    async def test_heartbeat_put_timeout_records_wedge(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The heartbeat get succeeds but the put times out on a degraded
        # connection: the put must be tracked so the wedge is visible, not
        # silently swallowed while the get emits a false recovery.
        relay = NatsRelay(url="tls://fake:4222", repo_name="test")
        relay._nc = _connected_nc()
        relay._health.record_connected(5.0, is_new_connection=True)

        entry = MagicMock()
        entry.value = UserSession(user="kai", tty="abc123").model_dump_json().encode()
        kv = MagicMock()
        kv.get = AsyncMock(return_value=entry)
        kv.put = AsyncMock(side_effect=TimeoutError)
        js = MagicMock()
        relay._ensure_connected = AsyncMock(return_value=(js, kv))  # type: ignore[method-assign]

        caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
        with pytest.raises(TimeoutError):
            await relay.heartbeat("kai:abc123")

        assert relay._health.consecutive_timeouts == 1
        assert _onset_records(caplog), "heartbeat put must route through _tracked"


class TestForceReconnectLatch:
    """The proactive force-reconnect fires once per wedge episode (biff-3hp).

    ``_ConnectionHealth`` owns the consecutive-timeout counter and the
    once-per-episode latch; ``NatsRelay._tracked`` acts on the decision.
    """

    def test_below_threshold_does_not_arm(self) -> None:
        health = _ConnectionHealth("tls://fake:4222")
        health.record_connected(5.0, is_new_connection=True)
        threshold = _WEDGE_FORCE_RECONNECT_THRESHOLD
        for _ in range(threshold - 1):
            health.record_timeout("stream_info", is_connected=True)
            assert not health.should_force_reconnect(threshold)

    def test_fires_exactly_once_at_threshold(self) -> None:
        health = _ConnectionHealth("tls://fake:4222")
        health.record_connected(5.0, is_new_connection=True)
        threshold = _WEDGE_FORCE_RECONNECT_THRESHOLD
        for _ in range(threshold):
            health.record_timeout("stream_info", is_connected=True)
        assert health.should_force_reconnect(threshold)
        # Latched — further timeouts past the threshold do not re-arm it.
        health.record_timeout("stream_info", is_connected=True)
        assert not health.should_force_reconnect(threshold)

    def test_success_clears_latch_for_next_episode(self) -> None:
        health = _ConnectionHealth("tls://fake:4222")
        health.record_connected(5.0, is_new_connection=True)
        threshold = _WEDGE_FORCE_RECONNECT_THRESHOLD
        for _ in range(threshold):
            health.record_timeout("stream_info", is_connected=True)
        assert health.should_force_reconnect(threshold)
        health.record_success()
        assert health.consecutive_timeouts == 0
        # A fresh wedge episode re-arms and fires again.
        for _ in range(threshold):
            health.record_timeout("stream_info", is_connected=True)
        assert health.should_force_reconnect(threshold)

    def test_disconnect_clears_wedge_counter_and_latch(self) -> None:
        # When keepalive's _on_disconnect fires, the counter resets so the
        # proactive path won't also fire (nats-py already owns recovery).
        health = _ConnectionHealth("tls://fake:4222")
        health.record_connected(5.0, is_new_connection=True)
        for _ in range(_WEDGE_FORCE_RECONNECT_THRESHOLD):
            health.record_timeout("stream_info", is_connected=True)
        health.record_disconnected()
        assert health.consecutive_timeouts == 0
        assert not health.should_force_reconnect(_WEDGE_FORCE_RECONNECT_THRESHOLD)

    def test_closed_clears_wedge_counter_and_latch(self) -> None:
        # Defense-in-depth: record_closed is a lifecycle reset point too — the
        # latch must not outlive a close, independent of whether _on_closed
        # nulled the handles.  Symmetric with connect/disconnect/reconnect.
        health = _ConnectionHealth("tls://fake:4222")
        health.record_connected(5.0, is_new_connection=True)
        for _ in range(_WEDGE_FORCE_RECONNECT_THRESHOLD):
            health.record_timeout("stream_info", is_connected=True)
        assert health.should_force_reconnect(_WEDGE_FORCE_RECONNECT_THRESHOLD)
        health.record_closed()
        assert health.consecutive_timeouts == 0
        assert not health.should_force_reconnect(_WEDGE_FORCE_RECONNECT_THRESHOLD)


class TestLifecycleContext:
    """connect / disconnect / reconnect carry lifetime and downtime context."""

    @pytest.fixture()
    def clock(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Clock]:
        clock = _Clock()
        monkeypatch.setattr(time, "monotonic", clock)
        yield clock

    def test_connect_reports_provision_time(
        self, caplog: pytest.LogCaptureFixture, clock: _Clock
    ) -> None:
        health = _ConnectionHealth("tls://connect.ngs.global")
        caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
        clock.set(1000.0)
        health.record_connected(42.0, is_new_connection=True)

        (record,) = [r for r in caplog.records if "Connected to NATS" in r.getMessage()]
        message = record.getMessage()
        assert record.levelno == logging.INFO
        assert "connect.ngs.global" in message
        assert "provisioned in 42ms" in message

    def test_disconnect_reports_connection_lifetime(
        self, caplog: pytest.LogCaptureFixture, clock: _Clock
    ) -> None:
        health = _ConnectionHealth("tls://connect.ngs.global")
        clock.set(1000.0)
        health.record_connected(5.0, is_new_connection=True)
        caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
        clock.set(1030.0)
        health.record_disconnected()

        (record,) = [r for r in caplog.records if "Disconnected" in r.getMessage()]
        # INFO, not WARNING: a disconnect is transient/auto-recovering and must
        # stay off the CLI terminal — file only (biff-9la).
        assert record.levelno == logging.INFO
        assert "after 30s connected" in record.getMessage()

    def test_reconnect_reports_downtime(
        self, caplog: pytest.LogCaptureFixture, clock: _Clock
    ) -> None:
        health = _ConnectionHealth("tls://connect.ngs.global")
        clock.set(1000.0)
        health.record_connected(5.0, is_new_connection=True)
        clock.set(1030.0)
        health.record_disconnected()
        caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
        clock.set(1032.0)
        health.record_reconnected()

        (record,) = [r for r in caplog.records if "Reconnected" in r.getMessage()]
        assert record.levelno == logging.INFO
        assert "after 2s down" in record.getMessage()

    def test_reprovision_after_reconnect_is_silent(
        self, caplog: pytest.LogCaptureFixture, clock: _Clock
    ) -> None:
        health = _ConnectionHealth("tls://connect.ngs.global")
        clock.set(1000.0)
        health.record_connected(5.0, is_new_connection=True)
        caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
        # A re-provision on an already-reconnected client must not double-log.
        health.record_connected(5.0, is_new_connection=False)
        assert not [r for r in caplog.records if "Connected to NATS" in r.getMessage()]


class TestNoContentLeak:
    """Connection-cluster log lines never carry message body or plan text."""

    @pytest.mark.anyio()
    async def test_wedge_during_deliver_omits_body(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        secret = "SECRET_BODY_do_not_log_me_9f3a"
        relay = NatsRelay(url="tls://fake:4222", repo_name="test")
        relay._nc = _connected_nc()
        relay._health.record_connected(5.0, is_new_connection=True)

        js = MagicMock()
        js.publish = AsyncMock(side_effect=TimeoutError)
        kv = MagicMock()
        relay._ensure_connected = AsyncMock(return_value=(js, kv))  # type: ignore[method-assign]

        message = Message(from_user="kai", to_user="eric", body=secret)
        caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
        with pytest.raises(TimeoutError):
            await relay.deliver(message)

        # The onset warning must have fired (the wrapped publish timed out) ...
        assert _onset_records(caplog), "publish path must route through _tracked"
        # ... but no captured log line may contain the message body.
        for record in caplog.records:
            assert secret not in record.getMessage()

    def test_url_credentials_are_never_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        health = _ConnectionHealth("nats://user:sup3rsecret@host.example:4222")
        caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
        health.record_connected(5.0, is_new_connection=True)

        (record,) = [r for r in caplog.records if "Connected to NATS" in r.getMessage()]
        message = record.getMessage()
        assert "sup3rsecret" not in message
        assert "user:" not in message
        assert "host.example:4222" in message

    def test_scheme_less_url_with_credentials_is_never_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # relay_url is free-form config: a scheme is not required, so a
        # scheme-less "user:pass@host" form is reachable and must not leak.
        health = _ConnectionHealth("user:sup3rsecret@host.example:4222")
        caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
        health.record_connected(5.0, is_new_connection=True)

        (record,) = [r for r in caplog.records if "Connected to NATS" in r.getMessage()]
        message = record.getMessage()
        assert "sup3rsecret" not in message
        assert "user:" not in message
        assert "host.example:4222" in message

    def test_cluster_url_does_not_crash_and_logs_first_host(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A comma-separated cluster list must not raise on construction
        # (urlsplit(...).port on the raw list raises ValueError).
        health = _ConnectionHealth("tls://h1.example:4222,tls://h2.example:5222")
        caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
        health.record_connected(5.0, is_new_connection=True)

        (record,) = [r for r in caplog.records if "Connected to NATS" in r.getMessage()]
        message = record.getMessage()
        assert "h1.example:4222" in message
        assert "h2.example" not in message  # first server only

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            # scheme-less creds: strip userinfo, keep host:port
            ("user:sup3rsecret@host.example:4222", "host.example:4222"),
            # comma-separated cluster: first server only
            ("tls://h1.example:4222,tls://h2.example:5222", "h1.example:4222"),
            # IPv6 with port: brackets + port
            ("nats://[2001:db8::1]:4222", "[2001:db8::1]:4222"),
            # IPv6 without port: brackets regardless of port (Copilot edge)
            ("nats://[2001:db8::1]", "[2001:db8::1]"),
            # explicit :0 port must survive (truthiness would drop it)
            ("nats://host:0", "host:0"),
            # ordinary host, no port
            ("tls://connect.ngs.global", "connect.ngs.global"),
            # unparseable: placeholder, never the raw (credentialed) url
            ("://:::", "<unknown host>"),
        ],
    )
    def test_host_of_covers_all_forms(self, url: str, expected: str) -> None:
        assert _ConnectionHealth._host_of(url) == expected
