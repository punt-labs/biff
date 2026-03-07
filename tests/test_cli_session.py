"""Tests for the CLI session lifecycle (biff.cli_session).

Tests the ``CliContext`` dataclass and ``cli_session()`` lifecycle
using mocked relays — no real NATS connection required.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from biff.cli_session import CliContext, _heartbeat_loop, cli_session
from biff.models import BiffConfig, UserSession


class TestCliContext:
    def test_frozen(self) -> None:
        ctx = CliContext(
            relay=object(),  # type: ignore[arg-type]
            config=BiffConfig(user="kai", repo_name="test"),
            session_key="kai:abc12345",
            user="kai",
            tty="abc12345",
            tty_name="tty1",
        )
        assert ctx.user == "kai"
        assert ctx.tty_name == "tty1"
        assert ctx.session_key == "kai:abc12345"

    def test_default_tty_name(self) -> None:
        ctx = CliContext(
            relay=object(),  # type: ignore[arg-type]
            config=BiffConfig(user="kai", repo_name="test"),
            session_key="kai:abc12345",
            user="kai",
            tty="abc12345",
        )
        assert ctx.tty_name == ""


class TestHeartbeatLoop:
    @pytest.mark.anyio()
    async def test_stops_on_shutdown(self) -> None:
        relay = AsyncMock()
        shutdown = asyncio.Event()
        shutdown.set()  # Immediate shutdown
        await _heartbeat_loop(relay, "kai:abc", shutdown, interval=0.01)
        relay.heartbeat.assert_not_awaited()

    @pytest.mark.anyio()
    async def test_fires_heartbeat(self) -> None:
        relay = AsyncMock()
        shutdown = asyncio.Event()

        async def _stop_after_one() -> None:
            await asyncio.sleep(0.05)
            shutdown.set()

        task = asyncio.create_task(_stop_after_one())
        await _heartbeat_loop(relay, "kai:abc", shutdown, interval=0.01)
        assert relay.heartbeat.await_count >= 1
        relay.heartbeat.assert_awaited_with("kai:abc")
        await task

    @pytest.mark.anyio()
    async def test_survives_cancelled_error(self) -> None:
        """CancelledError during heartbeat returns cleanly, no warning."""
        relay = AsyncMock()
        relay.heartbeat.side_effect = asyncio.CancelledError
        shutdown = asyncio.Event()
        # Should return without raising
        await _heartbeat_loop(relay, "kai:abc", shutdown, interval=0.01)


class TestCliSessionLifecycle:
    """Test cli_session() using a mock NatsRelay.

    We mock the NatsRelay constructor and its methods to test the
    lifecycle without a real NATS connection.
    """

    @pytest.fixture()
    def mock_relay(self) -> AsyncMock:
        relay = AsyncMock()
        relay.get_sessions.return_value = [
            UserSession(user="eric", tty="11111111", tty_name="tty1"),
        ]
        relay.get_session.return_value = None
        relay.flush = AsyncMock()
        relay.close = AsyncMock()
        return relay

    @pytest.fixture()
    def mock_config(self, tmp_path: Path) -> object:
        from biff.config import ResolvedConfig

        return ResolvedConfig(
            config=BiffConfig(
                user="kai",
                repo_name="test",
                relay_url="nats://localhost:4222",
            ),
            data_dir=tmp_path,
            repo_root=tmp_path,
        )

    @pytest.mark.anyio()
    async def test_session_registered_and_deleted(
        self, mock_relay: AsyncMock, mock_config: object
    ) -> None:
        with (
            patch("biff.cli_session.load_config", return_value=mock_config),
            patch("biff.cli_session.NatsRelay", return_value=mock_relay),
        ):
            async with cli_session() as ctx:
                assert ctx.user == "kai"
                assert ctx.tty_name == "tty2"  # tty1 taken by eric

        # Session was registered
        mock_relay.update_session.assert_awaited()
        registered = mock_relay.update_session.call_args_list[0][0][0]
        assert registered.user == "kai"
        assert registered.tty_name == "tty2"

        # Session was deleted on exit
        mock_relay.delete_session.assert_awaited_once()

        # Relay was closed
        mock_relay.close.assert_awaited_once()

    @pytest.mark.anyio()
    async def test_wtmp_login_and_logout_written(
        self, mock_relay: AsyncMock, mock_config: object
    ) -> None:
        with (
            patch("biff.cli_session.load_config", return_value=mock_config),
            patch("biff.cli_session.NatsRelay", return_value=mock_relay),
        ):
            async with cli_session() as _ctx:
                pass

        # Two wtmp events: login + logout
        assert mock_relay.append_wtmp.await_count == 2
        login_event = mock_relay.append_wtmp.call_args_list[0][0][0]
        logout_event = mock_relay.append_wtmp.call_args_list[1][0][0]
        assert login_event.event == "login"
        assert logout_event.event == "logout"
        assert login_event.session_key == logout_event.session_key

    @pytest.mark.anyio()
    async def test_interactive_starts_heartbeat(
        self, mock_relay: AsyncMock, mock_config: object
    ) -> None:
        with (
            patch("biff.cli_session.load_config", return_value=mock_config),
            patch("biff.cli_session.NatsRelay", return_value=mock_relay),
        ):
            async with cli_session(interactive=True) as _ctx:
                # Allow the background heartbeat task to be started.
                await asyncio.sleep(0.05)

        # Verifies interactive sessions with a heartbeat task still
        # clean up the relay correctly when the context exits.
        mock_relay.close.assert_awaited_once()

    @pytest.mark.anyio()
    async def test_cleanup_on_setup_failure(
        self, mock_relay: AsyncMock, mock_config: object
    ) -> None:
        """relay.close() is called even if session registration fails."""
        mock_relay.update_session.side_effect = ConnectionError("NATS down")

        with (
            patch("biff.cli_session.load_config", return_value=mock_config),
            patch("biff.cli_session.NatsRelay", return_value=mock_relay),
            pytest.raises(ConnectionError, match="NATS down"),
        ):
            async with cli_session() as _ctx:
                pass  # Should not reach here

        # Relay still closed despite failure
        mock_relay.close.assert_awaited_once()
        # No logout written (session was never registered)
        logout_calls = [
            c
            for c in mock_relay.append_wtmp.call_args_list
            if c[0][0].event == "logout"
        ]
        assert len(logout_calls) == 0

    @pytest.mark.anyio()
    async def test_no_relay_url_raises(self, tmp_path: Path) -> None:
        from biff.config import ResolvedConfig

        no_relay = ResolvedConfig(
            config=BiffConfig(user="kai", repo_name="test"),
            data_dir=tmp_path,
            repo_root=tmp_path,
        )
        with (
            patch("biff.cli_session.load_config", return_value=no_relay),
            pytest.raises(ValueError, match="NATS relay"),
        ):
            async with cli_session() as _ctx:
                pass
