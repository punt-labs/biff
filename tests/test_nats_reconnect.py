"""Unit tests for NatsRelay connection-wedge recovery (biff-wr3).

Regression coverage for the production incident where a NATS
``unexpected EOF`` sent every MCP server into a state where
``_open_connection`` re-provisioned JetStream/KV on a disconnected
connection with no timeout — blocking forever while holding
``_connect_lock`` and wedging every relay caller.

These tests mock the connection, so they run in tiers 1-2 (no
``nats`` marker, no real server).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from biff.nats_relay import NatsRelay


def _fake_nc() -> MagicMock:
    """A NATS client stand-in that is open but whose close() is awaitable."""
    nc = MagicMock()
    nc.is_closed = False
    nc.close = AsyncMock()
    return nc


def _fresh_nc(*_a: object, **_k: object) -> MagicMock:
    """side_effect for nats.connect — hand back a new fake client per call."""
    return _fake_nc()


class TestProvisionTimeout:
    """A blocked provision must not hold _connect_lock forever."""

    @pytest.mark.anyio()
    async def test_blocked_provision_times_out_and_resets(self) -> None:
        relay = NatsRelay(url="tls://fake:4222", repo_name="test")
        nc = _fake_nc()

        async def _hang(_nc: Any) -> tuple[Any, Any, Any]:
            await asyncio.Event().wait()  # never completes — simulates a dead conn
            raise AssertionError("unreachable")

        with (
            patch("biff.nats_relay.nats.connect", AsyncMock(return_value=nc)),
            patch("biff.nats_relay._CONNECT_PROVISION_TIMEOUT", 0.05),
            patch.object(relay, "_provision", _hang),
            pytest.raises(TimeoutError),
        ):
            await relay._ensure_connected()

        # The wedged connection is torn down so the next call reconnects fresh.
        assert relay._nc is None
        nc.close.assert_awaited_once()

    @pytest.mark.anyio()
    async def test_lock_released_after_timeout(self) -> None:
        """After a provision timeout the lock is free for the next caller."""
        relay = NatsRelay(url="tls://fake:4222", repo_name="test")
        nc = _fake_nc()

        async def _hang(_nc: Any) -> tuple[Any, Any, Any]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        with (
            patch("biff.nats_relay.nats.connect", AsyncMock(return_value=nc)),
            patch("biff.nats_relay._CONNECT_PROVISION_TIMEOUT", 0.05),
            patch.object(relay, "_provision", _hang),
            pytest.raises(TimeoutError),
        ):
            await relay._ensure_connected()

        assert not relay._connect_lock.locked()

    @pytest.mark.anyio()
    async def test_recovers_on_next_call_after_timeout(self) -> None:
        """A transient block times out; a later healthy provision succeeds."""
        relay = NatsRelay(url="tls://fake:4222", repo_name="test")
        js, kv, names_kv = MagicMock(), MagicMock(), MagicMock()
        calls = {"n": 0}

        async def _flaky(_nc: Any) -> tuple[Any, Any, Any]:
            calls["n"] += 1
            if calls["n"] == 1:
                await asyncio.Event().wait()  # first attempt wedges
                raise AssertionError("unreachable")
            return js, kv, names_kv  # second attempt succeeds

        with (
            patch(
                "biff.nats_relay.nats.connect",
                AsyncMock(side_effect=_fresh_nc),
            ),
            patch("biff.nats_relay._CONNECT_PROVISION_TIMEOUT", 0.05),
            patch.object(relay, "_provision", _flaky),
        ):
            with pytest.raises(TimeoutError):
                await relay._ensure_connected()
            # Second call reconnects and provisions cleanly.
            result_js, result_kv = await relay._ensure_connected()

        assert result_js is js
        assert result_kv is kv
        assert relay._nc is not None


class TestHalfOpenWedgeRecovery:
    """A half-open connection must be detected and recovered, not looped on.

    Regression coverage for biff-tww (DES-030): the NATS socket stays up but
    the server stops responding, so every JetStream/KV request raises
    ``nats: timeout``.  nats-py's default keepalive
    (ping_interval=120s, max_outstanding_pings=2) only declares such a
    connection dead after ~240s — during which the poller and heartbeat
    crash-loop with no recovery.  The fix tunes keepalive so detection
    happens in ~60s, firing nats-py's own reconnect + handle invalidation.
    """

    @staticmethod
    def _connect_and_provision(
        relay: NatsRelay, handles: tuple[Any, Any, Any]
    ) -> AsyncMock:
        """Patch ``nats.connect`` (fresh client per call) and ``_provision``."""
        connect = AsyncMock(side_effect=_fresh_nc)
        relay._provision = AsyncMock(return_value=handles)  # type: ignore[method-assign]
        return connect

    @pytest.mark.anyio()
    async def test_connect_uses_bounded_keepalive(self) -> None:
        """Detection latency must be far below the 240s nats-py default.

        nats-py's ping timer cannot be advanced in a unit test, so the fix
        is verified at its source: the keepalive parameters passed to
        ``nats.connect``.  ``ping_interval * max_outstanding_pings`` bounds
        how long a half-open connection can wedge before reconnect fires.
        """
        relay = NatsRelay(url="tls://fake:4222", repo_name="test")
        connect = self._connect_and_provision(
            relay, (MagicMock(), MagicMock(), MagicMock())
        )
        with patch("biff.nats_relay.nats.connect", connect):
            await relay._ensure_connected()

        assert connect.await_args is not None
        kwargs = connect.await_args.kwargs
        detection = kwargs["ping_interval"] * kwargs["max_outstanding_pings"]
        assert detection <= 90, f"wedge detection {detection}s exceeds 90s budget"
        # Never give up mid-outage — a bounded count strands the MCP server.
        assert kwargs["max_reconnect_attempts"] == -1

    @pytest.mark.anyio()
    async def test_wedge_detection_invalidates_and_recovers(self) -> None:
        """Ping detection fires disconnect → handles cleared → next call rebuilds.

        Models the sequence prompt keepalive enables: nats-py declares the
        half-open connection dead and invokes ``disconnected_cb``, which
        invalidates cached handles (DES-029); the next relay call then
        rebuilds them on the reconnected client instead of looping forever.
        """
        relay = NatsRelay(url="tls://fake:4222", repo_name="test")
        js, kv, names_kv = MagicMock(), MagicMock(), MagicMock()
        connect = self._connect_and_provision(relay, (js, kv, names_kv))
        with patch("biff.nats_relay.nats.connect", connect):
            await relay._ensure_connected()
            assert relay._cached_handles() is not None

            # nats-py's ping loop detects the dead socket and fires the
            # disconnect callback it was registered with.
            assert connect.await_args is not None
            on_disconnect = connect.await_args.kwargs["disconnected_cb"]
            await on_disconnect()
            assert relay._cached_handles() is None

            # The next call rebuilds handles rather than reusing the wedge.
            result_js, result_kv = await relay._ensure_connected()

        assert result_js is js
        assert result_kv is kv
