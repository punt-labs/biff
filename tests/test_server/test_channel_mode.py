"""Channel mode for the Claude Code plugin (DES-087): the unit-level contract.

The wire behavior through the real ``biff mcp`` process lives in
``tests/test_subprocess/test_channel_mode.py``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest
from mcp.server.session import ServerSession
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_REQUEST, PROTOCOL_VERSION_META_KEY
from typer.testing import CliRunner

from biff.__main__ import app
from biff.server import channel, channel_server
from biff.server.channel import (
    CHANNEL_METHOD,
    CHANNEL_MODE_VARIABLE,
    ChannelLink,
    ChannelUnavailableError,
    take_channel_mode,
)
from biff.server.channel_server import ChannelServer, _HandshakeOnly
from biff.server.tools import _descriptions

if TYPE_CHECKING:
    from collections.abc import Generator

runner = CliRunner()


class _Outbound:
    """Records notifications; optionally fails or stalls like a bad stream."""

    def __init__(self, *, fail: Exception | None = None, stall: bool = False) -> None:
        self.sent: list[tuple[str, Mapping[str, object] | None]] = []
        self._fail = fail
        self._stall = stall

    async def notify(self, method: str, params: Mapping[str, object] | None) -> None:
        if self._fail is not None:
            raise self._fail
        if self._stall:
            await asyncio.Event().wait()
        self.sent.append((method, params))


def _connection(outbound: _Outbound) -> MagicMock:
    connection = MagicMock()
    connection.outbound = outbound
    return connection


class TestTakeChannelMode:
    """``biff mcp`` reads the variable once and removes it."""

    def test_one_turns_the_mode_on_and_removes_the_variable(self) -> None:
        environ = {CHANNEL_MODE_VARIABLE: "1", "HOME": "/h"}
        assert take_channel_mode(environ) is True
        assert environ == {"HOME": "/h"}

    @pytest.mark.parametrize("value", ["0", "", "true", "yes"])
    def test_any_other_value_is_off_but_still_removed(self, value: str) -> None:
        environ = {CHANNEL_MODE_VARIABLE: value}
        assert take_channel_mode(environ) is False
        assert environ == {}

    def test_absent_is_off(self) -> None:
        assert take_channel_mode({}) is False

    def test_defaults_to_the_process_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CHANNEL_MODE_VARIABLE, "1")
        assert take_channel_mode() is True
        assert CHANNEL_MODE_VARIABLE not in os.environ


class TestChannelLink:
    """The narrow send capability the channel sender will use."""

    async def test_sends_the_channel_notification(self) -> None:
        outbound = _Outbound()
        link = ChannelLink()
        link.bind(_connection(outbound))

        await link.send("[WALL] hello", {"source": "biff"})

        assert outbound.sent == [
            (CHANNEL_METHOD, {"content": "[WALL] hello", "meta": {"source": "biff"}})
        ]

    async def test_unbound_send_raises(self) -> None:
        with pytest.raises(ChannelUnavailableError):
            await ChannelLink().send("x", {})

    async def test_unbind_detaches(self) -> None:
        link = ChannelLink()
        link.bind(_connection(_Outbound()))
        link.unbind()
        assert not link.is_bound

    @pytest.mark.parametrize(
        "error", [anyio.ClosedResourceError(), anyio.BrokenResourceError()]
    )
    async def test_closed_stream_is_reported_not_swallowed(
        self, error: Exception
    ) -> None:
        link = ChannelLink()
        link.bind(_connection(_Outbound(fail=error)))
        with pytest.raises(ChannelUnavailableError):
            await link.send("x", {})

    async def test_stalled_write_times_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(channel, "CHANNEL_SEND_TIMEOUT", 0.05)
        link = ChannelLink()
        link.bind(_connection(_Outbound(stall=True)))
        with pytest.raises(TimeoutError):
            await link.send("x", {})


async def _stall() -> None:
    await asyncio.Event().wait()


@pytest.fixture
def stalled_session() -> MagicMock:
    session = MagicMock(spec=ServerSession, protocol_version="2025-11-25")
    session.send_tool_list_changed = AsyncMock(side_effect=_stall)
    return session


class TestTimedOutSendKeepsSession:
    """DES-061's ``_notify_standalone`` amended for channel mode (DES-087)."""

    @pytest.fixture(autouse=True)
    def _fresh_delivery(self, monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
        _descriptions._reset_session()
        monkeypatch.setattr(_descriptions, "_NOTIFY_SEND_TIMEOUT", 0.05)
        yield
        _descriptions._reset_session()

    async def test_default_clears_the_session_on_timeout(
        self, stalled_session: MagicMock
    ) -> None:
        delivery = _descriptions.handshake_delivery
        await delivery.capture_session(stalled_session)
        await delivery.notify()

        assert delivery._session is None
        assert delivery._pending_notify

    async def test_channel_mode_keeps_the_session_on_timeout(
        self, stalled_session: MagicMock
    ) -> None:
        delivery = _descriptions.handshake_delivery
        delivery.keep_session = True
        await delivery.capture_session(stalled_session)
        await delivery.notify()

        assert delivery._session is stalled_session
        assert delivery._pending_notify

    async def test_channel_mode_keeps_the_session_on_any_failed_send(self) -> None:
        delivery = _descriptions.handshake_delivery
        delivery.keep_session = True
        broken = MagicMock(spec=ServerSession, protocol_version="2025-11-25")
        broken.send_tool_list_changed = AsyncMock(side_effect=RuntimeError("gone"))
        await delivery.capture_session(broken)
        await delivery.notify()

        assert delivery._session is broken

    async def test_channel_mode_resends_after_a_timeout(
        self, stalled_session: MagicMock
    ) -> None:
        delivery = _descriptions.handshake_delivery
        delivery.keep_session = True
        await delivery.capture_session(stalled_session)
        await delivery.notify()
        stalled_session.send_tool_list_changed = AsyncMock()
        await delivery.notify()

        stalled_session.send_tool_list_changed.assert_awaited_once()
        assert not delivery._pending_notify


class TestHandshakeOnly:
    """The channel loop's request gate: refuse 2026-07-28, log the handshake."""

    @staticmethod
    def _runner(version: str = "2025-11-25") -> MagicMock:
        runner = MagicMock()
        runner.on_request = AsyncMock(return_value={"ok": True})
        runner.connection.protocol_version = version
        return runner

    async def test_initialize_logs_the_negotiated_protocol_version(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        gate = _HandshakeOnly(self._runner())
        with caplog.at_level(logging.INFO, logger=channel_server.logger.name):
            await gate.on_request(MagicMock(), "initialize", {})

        assert "protocolVersion=2025-11-25 channel_mode=on" in caplog.text

    async def test_modern_request_is_refused_without_reaching_the_runner(
        self,
    ) -> None:
        runner = self._runner()
        gate = _HandshakeOnly(runner)
        params = {"_meta": {PROTOCOL_VERSION_META_KEY: "2026-07-28"}}

        with pytest.raises(MCPError) as refused:
            await gate.on_request(MagicMock(), "tools/list", params)

        assert refused.value.code == INVALID_REQUEST
        runner.on_request.assert_not_awaited()

    async def test_probe_reaches_the_runner_which_answers_method_not_found(
        self,
    ) -> None:
        runner = self._runner()
        params = {"_meta": {PROTOCOL_VERSION_META_KEY: "2026-07-28"}}

        await _HandshakeOnly(runner).on_request(MagicMock(), "server/discover", params)

        runner.on_request.assert_awaited_once()

    async def test_handshake_era_request_passes_through(self) -> None:
        runner = self._runner()

        result = await _HandshakeOnly(runner).on_request(MagicMock(), "tools/list", {})

        assert result == {"ok": True}


_SERVER_ARGS = ["mcp", "--user", "kai"]


class TestMcpEntry:
    """``biff mcp`` chooses the runner from the variable, once."""

    @pytest.fixture(autouse=True)
    def _clean_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(CHANNEL_MODE_VARIABLE, raising=False)
        _descriptions._reset_session()

    @staticmethod
    def _invoke(channel_calls: list[str], dual_era_calls: list[dict[str, str]]) -> int:
        async def serve_channel(
            _self: ChannelServer[object], link: ChannelLink
        ) -> None:
            assert isinstance(link, ChannelLink)
            channel_calls.append(
                "absent" if CHANNEL_MODE_VARIABLE not in os.environ else "set"
            )

        def run(_self: ChannelServer[object], **kwargs: str) -> None:
            dual_era_calls.append(kwargs)

        with (
            patch(
                "biff.server.transport_runtime.create_server",
                return_value=ChannelServer[object]("biff"),
            ),
            patch("biff.server.launch.create_state"),
            patch("biff.server.entrypoints.load_mcp_config"),
            patch.object(ChannelServer, "serve_channel_stdio", serve_channel),
            patch.object(ChannelServer, "run", run),
        ):
            return runner.invoke(app, _SERVER_ARGS).exit_code

    def test_variable_selects_the_channel_runner_and_is_gone_inside_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CHANNEL_MODE_VARIABLE, "1")
        channel_calls: list[str] = []
        dual_era_calls: list[dict[str, str]] = []

        assert self._invoke(channel_calls, dual_era_calls) == 0

        assert channel_calls == ["absent"]
        assert dual_era_calls == []

    def test_without_the_variable_the_dual_era_runner_is_unchanged(self) -> None:
        channel_calls: list[str] = []
        dual_era_calls: list[dict[str, str]] = []

        assert self._invoke(channel_calls, dual_era_calls) == 0

        assert channel_calls == []
        assert dual_era_calls == [{"transport": "stdio"}]
