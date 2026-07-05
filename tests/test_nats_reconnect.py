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
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from nats.js.errors import KeyNotFoundError

from biff.nats_relay import _WEDGE_FORCE_RECONNECT_THRESHOLD, NatsRelay

_LOGGER_NAME = "biff.nats_relay"


def _fake_nc() -> MagicMock:
    """A NATS client stand-in that is open but whose close() is awaitable."""
    nc = MagicMock()
    nc.is_closed = False
    nc.close = AsyncMock()
    return nc


def _live_nc() -> MagicMock:
    """A NATS client stand-in reporting a live, responsive connection."""
    nc = _fake_nc()
    nc.is_connected = True
    return nc


def _wedged_relay() -> tuple[NatsRelay, MagicMock]:
    """A relay with cached handles on a live-looking (half-open) client."""
    relay = NatsRelay(url="tls://fake:4222", repo_name="test")
    nc = _live_nc()
    relay._nc = nc
    relay._js = MagicMock()
    relay._kv = MagicMock()
    relay._names_kv = MagicMock()
    relay._health.record_connected(5.0, is_new_connection=True)
    return relay, nc


async def _timeout() -> str:
    raise TimeoutError


async def _ok() -> str:
    return "ok"


async def _keynotfound() -> str:
    raise KeyNotFoundError


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

    Regression coverage for biff-tww (DES-041): the NATS socket stays up but
    the server stops responding, so every JetStream/KV request raises
    ``nats: timeout``.  nats-py's default keepalive
    (ping_interval=120s, max_outstanding_pings=2) only declares such a
    connection dead after ~240s — during which the poller and heartbeat
    crash-loop with no recovery.  The fix tunes keepalive so detection
    happens in ~60-80s, firing nats-py's own reconnect + handle invalidation.
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
        ``nats.connect``.  ``ping_interval * max_outstanding_pings`` (~60s)
        is the base detection budget; the real trip is up to one interval
        higher (~80s) because nats-py fires when the count *exceeds* the
        max, which the 90s ceiling accommodates.
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


class TestProactiveWedgeDetector:
    """N consecutive runtime timeouts force a reconnect without keepalive (biff-3hp).

    The keepalive path (DES-041) takes ~60-80s to detect a half-open
    connection.  The proactive detector tears the connection down after
    ``_WEDGE_FORCE_RECONNECT_THRESHOLD`` consecutive JS/KV timeouts on a
    still-connected socket, so the next ``_ensure_connected`` dials a fresh
    client in ~N x per-request-timeout instead.
    """

    @pytest.mark.anyio()
    async def test_threshold_timeouts_force_one_reconnect(self) -> None:
        relay, nc = _wedged_relay()

        for _ in range(_WEDGE_FORCE_RECONNECT_THRESHOLD):
            with pytest.raises(TimeoutError):
                await relay._tracked("stream_info", _timeout())

        # Connection torn down and handles cleared so the next call rebuilds.
        nc.close.assert_awaited_once()
        assert relay._nc is None
        assert relay._js is None
        assert relay._kv is None
        assert relay._names_kv is None

    @pytest.mark.anyio()
    async def test_next_call_rebuilds_after_forced_reconnect(self) -> None:
        relay, _nc = _wedged_relay()
        for _ in range(_WEDGE_FORCE_RECONNECT_THRESHOLD):
            with pytest.raises(TimeoutError):
                await relay._tracked("stream_info", _timeout())
        assert relay._nc is None

        js, kv, names_kv = MagicMock(), MagicMock(), MagicMock()
        relay._provision = AsyncMock(return_value=(js, kv, names_kv))  # type: ignore[method-assign]
        with patch("biff.nats_relay.nats.connect", AsyncMock(side_effect=_fresh_nc)):
            result_js, result_kv = await relay._ensure_connected()

        assert result_js is js
        assert result_kv is kv
        assert relay._nc is not None

    @pytest.mark.anyio()
    async def test_below_threshold_then_success_does_not_reconnect(self) -> None:
        relay, nc = _wedged_relay()

        for _ in range(_WEDGE_FORCE_RECONNECT_THRESHOLD - 1):
            with pytest.raises(TimeoutError):
                await relay._tracked("stream_info", _timeout())
        # A single slow request that then recovers must not force a reconnect.
        assert await relay._tracked("stream_info", _ok()) == "ok"

        nc.close.assert_not_awaited()
        assert relay._nc is nc
        assert relay._health.consecutive_timeouts == 0

    @pytest.mark.anyio()
    async def test_reconnect_fires_once_per_episode(self) -> None:
        relay, nc = _wedged_relay()

        for _ in range(_WEDGE_FORCE_RECONNECT_THRESHOLD):
            with pytest.raises(TimeoutError):
                await relay._tracked("stream_info", _timeout())
        assert nc.close.await_count == 1
        assert relay._nc is None

        # Further timeouts on the torn-down connection must not tear down again.
        for _ in range(_WEDGE_FORCE_RECONNECT_THRESHOLD):
            with pytest.raises(TimeoutError):
                await relay._tracked("stream_info", _timeout())
        assert nc.close.await_count == 1

    @pytest.mark.anyio()
    async def test_reconnecting_socket_is_not_torn_down(self) -> None:
        # If nats-py's keepalive already fired (is_connected False, socket
        # mid-reconnect), the proactive path must not double-tear-down.
        relay, nc = _wedged_relay()
        nc.is_connected = False

        for _ in range(_WEDGE_FORCE_RECONNECT_THRESHOLD + 2):
            with pytest.raises(TimeoutError):
                await relay._tracked("stream_info", _timeout())

        nc.close.assert_not_awaited()
        assert relay._nc is nc

    @pytest.mark.anyio()
    async def test_defers_when_client_reconnects_under_lock(self) -> None:
        # Race: the _tracked gate saw is_connected=True and invoked
        # _force_reconnect, but nats-py's keepalive flips the captured client
        # to reconnecting (is_connected False, is_closed still False) before it
        # acquires _connect_lock.  The under-lock guard must defer to nats-py's
        # reconnect, not tear down a client already reconnecting.
        relay, nc = _wedged_relay()

        await relay._connect_lock.acquire()
        task = asyncio.ensure_future(relay._force_reconnect())
        await asyncio.sleep(0)  # let the task capture `wedged` and block on lock
        nc.is_connected = False  # keepalive fires while we hold the lock
        relay._connect_lock.release()
        await task

        nc.close.assert_not_awaited()
        assert relay._nc is nc
        assert relay._js is not None


class TestTimeoutIdentityGuard:
    """A ``_tracked`` timeout is charged to the client that owned the request.

    A JS/KV request can be pending when keepalive declares its client down and
    ``_ensure_connected`` dials a fresh one — swapping ``self._nc``.  If that
    pending request then times out, the timeout belongs to the *superseded*
    client, not the live one.  Charging it to the new client (and force-closing
    it) tears down a healthy connection (Copilot: tracked-timeout race).
    """

    @pytest.mark.anyio()
    async def test_timeout_on_superseded_client_does_not_reconnect(self) -> None:
        relay, _old_nc = _wedged_relay()

        # Bring the wedge counter to threshold-1 on the current (old) client.
        for _ in range(_WEDGE_FORCE_RECONNECT_THRESHOLD - 1):
            with pytest.raises(TimeoutError):
                await relay._tracked("stream_info", _timeout())
        assert (
            relay._health.consecutive_timeouts == _WEDGE_FORCE_RECONNECT_THRESHOLD - 1
        )

        new_nc = _live_nc()

        async def _swap_then_timeout() -> str:
            # Keepalive declared the old client down; _ensure_connected dialed
            # new_nc while this request was pending.  Now the request times out.
            relay._nc = new_nc
            raise TimeoutError

        with pytest.raises(TimeoutError):
            await relay._tracked("stream_info", _swap_then_timeout())

        # The timeout belonged to the superseded client: not charged, no
        # reconnect of the live client.
        new_nc.close.assert_not_awaited()
        assert relay._nc is new_nc
        assert (
            relay._health.consecutive_timeouts == _WEDGE_FORCE_RECONNECT_THRESHOLD - 1
        )

    @pytest.mark.anyio()
    async def test_timeout_on_current_client_still_reconnects(self) -> None:
        # Contrast: when the client is unchanged, threshold timeouts still force
        # a reconnect (existing behavior preserved).
        relay, nc = _wedged_relay()

        for _ in range(_WEDGE_FORCE_RECONNECT_THRESHOLD):
            with pytest.raises(TimeoutError):
                await relay._tracked("stream_info", _timeout())

        nc.close.assert_awaited_once()
        assert relay._nc is None

    @pytest.mark.anyio()
    async def test_success_on_superseded_client_does_not_clear_live_wedge(
        self,
    ) -> None:
        # Mirror concern: a slow success returning on a superseded client must
        # not clear the live client's wedge latch/counter.
        relay, _old_nc = _wedged_relay()

        for _ in range(_WEDGE_FORCE_RECONNECT_THRESHOLD - 1):
            with pytest.raises(TimeoutError):
                await relay._tracked("stream_info", _timeout())
        assert (
            relay._health.consecutive_timeouts == _WEDGE_FORCE_RECONNECT_THRESHOLD - 1
        )

        new_nc = _live_nc()

        async def _swap_then_ok() -> str:
            relay._nc = new_nc
            return "ok"

        assert await relay._tracked("stream_info", _swap_then_ok()) == "ok"

        # The success belonged to the superseded client: the live client's
        # wedge counter is untouched.
        assert (
            relay._health.consecutive_timeouts == _WEDGE_FORCE_RECONNECT_THRESHOLD - 1
        )

    @pytest.mark.anyio()
    async def test_success_on_current_client_still_clears_wedge(self) -> None:
        # A genuinely-current success still clears the latch and counter.
        relay, nc = _wedged_relay()

        for _ in range(_WEDGE_FORCE_RECONNECT_THRESHOLD - 1):
            with pytest.raises(TimeoutError):
                await relay._tracked("stream_info", _timeout())
        assert relay._health.consecutive_timeouts > 0

        assert await relay._tracked("stream_info", _ok()) == "ok"

        assert relay._nc is nc
        assert relay._health.consecutive_timeouts == 0

    @pytest.mark.anyio()
    async def test_keynotfound_on_superseded_client_does_not_clear_live_wedge(
        self,
    ) -> None:
        # A "not found" is proof of server liveness, so _tracked charges it as a
        # success.  But if the raising request belonged to a superseded client,
        # that liveness signal must not clear the live client's wedge latch —
        # the same identity guard the success/timeout paths use (Copilot:
        # tracked-timeout race).
        relay, _old_nc = _wedged_relay()

        for _ in range(_WEDGE_FORCE_RECONNECT_THRESHOLD - 1):
            with pytest.raises(TimeoutError):
                await relay._tracked("stream_info", _timeout())
        assert (
            relay._health.consecutive_timeouts == _WEDGE_FORCE_RECONNECT_THRESHOLD - 1
        )

        new_nc = _live_nc()

        async def _swap_then_keynotfound() -> str:
            relay._nc = new_nc
            raise KeyNotFoundError

        with pytest.raises(KeyNotFoundError):
            await relay._tracked("kv.get", _swap_then_keynotfound())

        # The liveness signal belonged to the superseded client: the live
        # client's wedge counter is untouched.
        assert (
            relay._health.consecutive_timeouts == _WEDGE_FORCE_RECONNECT_THRESHOLD - 1
        )

    @pytest.mark.anyio()
    async def test_keynotfound_on_current_client_still_clears_wedge(self) -> None:
        # Contrast: a KeyNotFoundError on the unchanged client is a live-server
        # answer and still clears the latch and counter.
        relay, nc = _wedged_relay()

        for _ in range(_WEDGE_FORCE_RECONNECT_THRESHOLD - 1):
            with pytest.raises(TimeoutError):
                await relay._tracked("stream_info", _timeout())
        assert relay._health.consecutive_timeouts > 0

        with pytest.raises(KeyNotFoundError):
            await relay._tracked("kv.get", _keynotfound())

        assert relay._nc is nc
        assert relay._health.consecutive_timeouts == 0


class TestCallbackGenerationGuard:
    """Stale lifecycle callbacks from a superseded client must not touch state.

    The connection callbacks (``_on_disconnect``/``_on_closed``) null
    ``self._nc`` and the cached handles.  ``_force_reconnect`` closes the
    wedged client and the next ``_ensure_connected`` dials a fresh one, so the
    OLD client's callbacks can still fire asynchronously.  A per-connection
    generation token makes a stale callback a no-op — it must never clear the
    handles of the LIVE connection (Bugbot Medium).
    """

    @staticmethod
    def _connect() -> AsyncMock:
        return AsyncMock(side_effect=_fresh_nc)

    @pytest.mark.anyio()
    async def test_stale_callbacks_do_not_null_live_connection(self) -> None:
        relay = NatsRelay(url="tls://fake:4222", repo_name="test")
        connect = self._connect()
        relay._provision = AsyncMock(  # type: ignore[method-assign]
            return_value=(MagicMock(), MagicMock(), MagicMock())
        )
        with patch("biff.nats_relay.nats.connect", connect):
            await relay._ensure_connected()  # client A, generation 1
            assert connect.await_args is not None
            on_disconnect_a = connect.await_args.kwargs["disconnected_cb"]
            on_closed_a = connect.await_args.kwargs["closed_cb"]

            # Supersede A with a fresh client B (force reconnect + rebuild).
            await relay._force_reconnect()
            js_b, kv_b, names_b = MagicMock(), MagicMock(), MagicMock()
            relay._provision = AsyncMock(return_value=(js_b, kv_b, names_b))  # type: ignore[method-assign]
            await relay._ensure_connected()  # client B, generation 2
            client_b = relay._nc
            assert client_b is not None

            # A's stale callbacks fire late — must be no-ops for B.
            await on_closed_a()
            await on_disconnect_a()

        assert relay._nc is client_b
        assert relay._js is js_b
        assert relay._kv is kv_b
        assert relay._names_kv is names_b

    @pytest.mark.anyio()
    async def test_current_close_callback_still_clears(self) -> None:
        # The legitimate case must keep working: a close of the CURRENT client
        # invalidates its _nc and handles.
        relay = NatsRelay(url="tls://fake:4222", repo_name="test")
        connect = self._connect()
        relay._provision = AsyncMock(  # type: ignore[method-assign]
            return_value=(MagicMock(), MagicMock(), MagicMock())
        )
        with patch("biff.nats_relay.nats.connect", connect):
            await relay._ensure_connected()
            assert relay._cached_handles() is not None
            assert connect.await_args is not None
            on_closed = connect.await_args.kwargs["closed_cb"]
            await on_closed()

        assert relay._nc is None
        assert relay._js is None
        assert relay._kv is None
        assert relay._names_kv is None

    @pytest.mark.anyio()
    async def test_current_disconnect_callback_still_clears_handles(self) -> None:
        # A disconnect of the CURRENT client invalidates handles (DES-029) but
        # keeps _nc so nats-py's reconnect can rebuild on the same client.
        relay = NatsRelay(url="tls://fake:4222", repo_name="test")
        connect = self._connect()
        relay._provision = AsyncMock(  # type: ignore[method-assign]
            return_value=(MagicMock(), MagicMock(), MagicMock())
        )
        with patch("biff.nats_relay.nats.connect", connect):
            await relay._ensure_connected()
            assert connect.await_args is not None
            on_disconnect = connect.await_args.kwargs["disconnected_cb"]
            await on_disconnect()

        assert relay._js is None
        assert relay._kv is None
        assert relay._names_kv is None
        assert relay._nc is not None

    @pytest.mark.anyio()
    async def test_stale_error_callback_no_ops(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A superseded client's error_cb must not log — it would blame the live
        # connection for a dead client's error (Copilot: ungeneration-guarded
        # error_cb).
        relay = NatsRelay(url="tls://fake:4222", repo_name="test")
        connect = self._connect()
        relay._provision = AsyncMock(  # type: ignore[method-assign]
            return_value=(MagicMock(), MagicMock(), MagicMock())
        )
        with patch("biff.nats_relay.nats.connect", connect):
            await relay._ensure_connected()  # client A, generation 1
            assert connect.await_args is not None
            on_error_a = connect.await_args.kwargs["error_cb"]

            await relay._force_reconnect()
            relay._provision = AsyncMock(  # type: ignore[method-assign]
                return_value=(MagicMock(), MagicMock(), MagicMock())
            )
            await relay._ensure_connected()  # client B, generation 2

            caplog.set_level(logging.ERROR, logger=_LOGGER_NAME)
            await on_error_a(RuntimeError("boom from superseded client"))

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert errors == [], "a superseded client's error_cb must no-op"

    @pytest.mark.anyio()
    async def test_current_error_callback_still_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The current generation's error_cb still logs the error.
        relay = NatsRelay(url="tls://fake:4222", repo_name="test")
        connect = self._connect()
        relay._provision = AsyncMock(  # type: ignore[method-assign]
            return_value=(MagicMock(), MagicMock(), MagicMock())
        )
        with patch("biff.nats_relay.nats.connect", connect):
            await relay._ensure_connected()
            assert connect.await_args is not None
            on_error = connect.await_args.kwargs["error_cb"]

            caplog.set_level(logging.ERROR, logger=_LOGGER_NAME)
            await on_error(RuntimeError("boom"))

        errors = [
            r
            for r in caplog.records
            if r.levelno == logging.ERROR and "boom" in r.getMessage()
        ]
        assert len(errors) == 1
