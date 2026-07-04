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

from biff.models import Message
from biff.nats_relay import NatsRelay, _ConnectionHealth

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
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "NATS request timed out" in r.getMessage()
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
        assert record.levelno == logging.WARNING
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
