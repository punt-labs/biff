"""Tests for the CLI session lifecycle (biff.cli_session).

Tests the ``CliContext`` dataclass and ``cli_session()`` lifecycle
using mocked relays — no real NATS connection required.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from biff.cli_context import CliContext
from biff.cli_session import _heartbeat_loop, cli_session
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
    async def test_renews_the_lease_on_each_tick(self) -> None:
        relay = AsyncMock()
        renew = AsyncMock()
        shutdown = asyncio.Event()

        async def _stop_after_one() -> None:
            await asyncio.sleep(0.05)
            shutdown.set()

        task = asyncio.create_task(_stop_after_one())
        await _heartbeat_loop(relay, "kai:abc", shutdown, interval=0.01, renew=renew)
        await task
        assert renew.await_count >= 1

    @pytest.mark.anyio()
    async def test_survives_cancelled_error(self) -> None:
        """CancelledError during heartbeat returns cleanly, no warning."""
        relay = AsyncMock()
        relay.heartbeat.side_effect = asyncio.CancelledError
        shutdown = asyncio.Event()
        # Should return without raising
        await _heartbeat_loop(relay, "kai:abc", shutdown, interval=0.01)

    @pytest.mark.anyio()
    async def test_transient_error_logs_at_info_not_terminal(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A transient heartbeat failure logs at INFO (with traceback) — never
        WARNING+ — so the REPL's WARNING-level stderr handler keeps it off the
        interactive terminal while the file handler still records it.
        """
        import logging

        relay = AsyncMock()
        relay.heartbeat.side_effect = TimeoutError("relay wedged")
        shutdown = asyncio.Event()

        async def _stop_after_one() -> None:
            await asyncio.sleep(0.05)
            shutdown.set()

        task = asyncio.create_task(_stop_after_one())
        with caplog.at_level(logging.INFO, logger="biff.cli_session"):
            await _heartbeat_loop(relay, "kai:abc", shutdown, interval=0.01)
        await task

        records = [r for r in caplog.records if r.name == "biff.cli_session"]
        assert records, "heartbeat failure was not logged at all"
        # Nothing at WARNING or above — that is what would reach the terminal.
        assert all(r.levelno < logging.WARNING for r in records)
        # The detail is preserved: INFO level, with the traceback attached.
        info = next(r for r in records if r.levelno == logging.INFO)
        assert info.exc_info is not None


class TestCliSessionLifecycle:
    """Test cli_session() using a mock NatsRelay.

    We mock the NatsRelay constructor and its methods to test the
    lifecycle without a real NATS connection.
    """

    @pytest.fixture(autouse=True)
    def _home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))

    @pytest.fixture()
    def mock_relay(self) -> AsyncMock:
        relay = AsyncMock()
        relay.get_sessions.return_value = [
            UserSession(user="eric", tty="11111111", tty_name="tty1"),
        ]
        relay.get_session.return_value = None
        relay.flush = AsyncMock()
        relay.close = AsyncMock()
        # DES-035: claim_tty_name uses relay reservation methods.
        relay.list_reserved_names.return_value = []
        relay.reserve_tty_name.return_value = True
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
            repo_common_root=tmp_path,
        )

    @pytest.mark.anyio()
    async def test_session_registered_and_kept(
        self, mock_relay: AsyncMock, mock_config: object
    ) -> None:
        with (
            patch("biff.cli_session.load_cli_config", return_value=mock_config),
            patch("biff.relay_select.NatsRelay", return_value=mock_relay),
        ):
            async with cli_session() as ctx:
                assert ctx.user == "kai"
                # DES-035: reservation-based, no session scan.
                assert ctx.tty_name == "tty1"

        # Session was registered
        mock_relay.update_session.assert_awaited()
        registered = mock_relay.update_session.call_args_list[0][0][0]
        assert registered.user == "kai"
        assert registered.tty_name == "tty1"

        # The session outlives the invocation: row and name are not released.
        mock_relay.delete_session.assert_not_awaited()
        mock_relay.release_tty_name.assert_not_awaited()

        # Relay was closed
        mock_relay.close.assert_awaited_once()

    @pytest.mark.anyio()
    async def test_wtmp_login_written_once_and_no_logout_on_exit(
        self, mock_relay: AsyncMock, mock_config: object
    ) -> None:
        with (
            patch("biff.cli_session.load_cli_config", return_value=mock_config),
            patch("biff.relay_select.NatsRelay", return_value=mock_relay),
        ):
            async with cli_session() as _ctx:
                pass

        # Only the creation is a login; detaching writes no logout.
        events = [c[0][0].event for c in mock_relay.append_wtmp.call_args_list]
        assert events == ["login"]

    @pytest.mark.anyio()
    async def test_interactive_starts_heartbeat(
        self, mock_relay: AsyncMock, mock_config: object
    ) -> None:
        with (
            patch("biff.cli_session.load_cli_config", return_value=mock_config),
            patch("biff.relay_select.NatsRelay", return_value=mock_relay),
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
            patch("biff.cli_session.load_cli_config", return_value=mock_config),
            patch("biff.relay_select.NatsRelay", return_value=mock_relay),
            pytest.raises(ConnectionError, match="NATS down"),
        ):
            async with cli_session() as _ctx:
                pass  # Should not reach here

        # Relay still closed despite failure
        mock_relay.close.assert_awaited_once()
        logout_calls = [
            c
            for c in mock_relay.append_wtmp.call_args_list
            if c[0][0].event == "logout"
        ]
        assert len(logout_calls) == 0

    @pytest.mark.anyio()
    async def test_registered_row_carries_launching_user(
        self, mock_relay: AsyncMock, tmp_path: Path
    ) -> None:
        """A CLI-registered row carries the OS user who launched it."""
        from biff.config import ResolvedConfig

        resolved = ResolvedConfig(
            config=BiffConfig(
                user="kai",
                launched_by="jim",
                repo_name="test",
                relay_url="nats://localhost:4222",
            ),
            data_dir=tmp_path,
            repo_root=tmp_path,
            repo_common_root=tmp_path,
        )
        with (
            patch("biff.cli_session.load_cli_config", return_value=resolved),
            patch("biff.relay_select.NatsRelay", return_value=mock_relay),
        ):
            async with cli_session() as _ctx:
                pass

        registered = mock_relay.update_session.call_args_list[0][0][0]
        assert registered.launched_by == "jim"

    @pytest.mark.anyio()
    async def test_no_relay_url_uses_the_local_relay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from biff.config import ResolvedConfig
        from biff.relay import LocalRelay

        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        no_relay = ResolvedConfig(
            config=BiffConfig(user="kai", repo_name="test"),
            data_dir=tmp_path / "relay",
            repo_root=tmp_path,
            repo_common_root=tmp_path,
        )
        with patch("biff.cli_session.load_cli_config", return_value=no_relay):
            async with cli_session() as ctx:
                assert isinstance(ctx.relay, LocalRelay)
                assert await ctx.relay.get_session(ctx.session_key) is not None


class TestSessionPersistence:
    """Two invocations from one shell are one session with one address."""

    @pytest.fixture()
    def env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> object:
        from biff.config import ResolvedConfig
        from biff.relay import LocalRelay

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        repo = tmp_path / "repo"
        repo.mkdir()
        resolved = ResolvedConfig(
            config=BiffConfig(
                user="kai", repo_name="test", relay_url="nats://localhost:4222"
            ),
            data_dir=tmp_path,
            repo_root=repo,
            repo_common_root=repo,
        )
        return resolved, LocalRelay(tmp_path / "relay")

    @pytest.mark.anyio()
    async def test_two_invocations_report_the_same_session(
        self, env: tuple[object, object]
    ) -> None:
        resolved, relay = env
        with (
            patch("biff.cli_session.load_cli_config", return_value=resolved),
            patch("biff.relay_select.NatsRelay", return_value=relay),
        ):
            async with cli_session() as first:
                pass
            async with cli_session() as second:
                pass

        assert first.session_key == second.session_key
        assert first.tty_name == second.tty_name

    @pytest.mark.anyio()
    async def test_relay_row_survives_exit(self, env: tuple[object, object]) -> None:
        resolved, relay = env
        with (
            patch("biff.cli_session.load_cli_config", return_value=resolved),
            patch("biff.relay_select.NatsRelay", return_value=relay),
        ):
            async with cli_session() as ctx:
                pass

        row = await relay.get_session(ctx.session_key)  # type: ignore[attr-defined]
        assert row is not None

    @pytest.mark.anyio()
    async def test_attached_cli_session_carries_its_record_id(
        self, env: tuple[object, object], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        resolved, relay = env
        with (
            patch("biff.cli_session.load_cli_config", return_value=resolved),
            patch("biff.relay_select.NatsRelay", return_value=relay),
        ):
            async with cli_session() as first:
                pass
            async with cli_session() as second:
                pass

        assert first.record_session_id is not None
        assert first.record_session_id.startswith("cli-")
        assert first.record_session_id == second.record_session_id
