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
