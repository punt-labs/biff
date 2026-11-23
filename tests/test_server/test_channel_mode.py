"""Channel mode for the Claude Code plugin (DES-087): the unit-level contract.

Streams here are the SDK's real ones (a ``JSONRPCDispatcher`` over memory
streams), because the SDK swallows a write onto a closed stream and a fake
that raises would prove nothing.  The wire behavior through the real
``biff mcp`` process lives in ``tests/test_subprocess/test_channel_mode.py``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest
from mcp.server.connection import Connection
from mcp.server.session import ServerSession
from mcp.shared.exceptions import MCPError
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from mcp.shared.message import SessionMessage
from mcp.types import (
    INVALID_REQUEST,
    PROTOCOL_VERSION_META_KEY,
    JSONRPCNotification,
)
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

    from anyio.abc import ObjectReceiveStream, ObjectSendStream
    from mcp.shared.transport_context import TransportContext

runner = CliRunner()


@dataclass(frozen=True, slots=True)
class _Wire:
    """A real dispatcher over memory streams, observed by a ``ChannelLink``."""

    link: ChannelLink
    dispatcher: JSONRPCDispatcher[TransportContext]
    to_client: ObjectReceiveStream[SessionMessage]
    to_server: ObjectSendStream[SessionMessage | Exception]
    server_end: ObjectSendStream[SessionMessage]

    @property
    def connection(self) -> Connection:
        """A handshake-era connection over the dispatcher."""
        return Connection.for_loop(self.dispatcher)


async def _no_request(*_args: object) -> dict[str, object]:
    return {}


async def _no_notify(*_args: object) -> None:
    return None


@asynccontextmanager
async def _wire(buffer: int = 10) -> AsyncGenerator[_Wire]:
    server_end, to_client = anyio.create_memory_object_stream[SessionMessage](buffer)
    to_server, from_client = anyio.create_memory_object_stream[
        SessionMessage | Exception
    ](buffer)
    link = ChannelLink()
    dispatcher: JSONRPCDispatcher[TransportContext] = JSONRPCDispatcher(
        from_client, link.observe(server_end)
    )
    async with anyio.create_task_group() as tg:
        tg.start_soon(dispatcher.run, _no_request, _no_notify)
        await anyio.sleep(0)
        yield _Wire(link, dispatcher, to_client, to_server, server_end)
        tg.cancel_scope.cancel()


class TestTakeChannelMode:
    """``biff mcp`` reads the variable once and removes it."""

    def test_one_turns_the_mode_on_and_removes_the_variable(self) -> None:
        environ = {CHANNEL_MODE_VARIABLE: "1", "HOME": "/h"}
        assert take_channel_mode(environ) is True
        assert environ == {"HOME": "/h"}

    @pytest.mark.parametrize("value", ["0", "", "true", "yes"])
    def test_any_other_value_is_off_but_still_removed(
        self, value: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        environ = {CHANNEL_MODE_VARIABLE: value}
        with caplog.at_level(logging.WARNING, logger=channel.logger.name):
            assert take_channel_mode(environ) is False
        assert environ == {}
        assert f"{CHANNEL_MODE_VARIABLE}={value!r}" in caplog.text

    def test_absent_is_off_and_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger=channel.logger.name):
            assert take_channel_mode({}) is False
        assert caplog.text == ""

    def test_defaults_to_the_process_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CHANNEL_MODE_VARIABLE, "1")
        assert take_channel_mode() is True
        assert CHANNEL_MODE_VARIABLE not in os.environ


class TestChannelLink:
    """The narrow send capability the channel sender will use."""

    async def test_sends_the_channel_notification(self) -> None:
        async with _wire() as wire:
            await wire.link.send("[WALL] hello", {"source": "biff"})

            frame = (await wire.to_client.receive()).message
        assert isinstance(frame, JSONRPCNotification)
        assert frame.method == CHANNEL_METHOD
        assert frame.params == {"content": "[WALL] hello", "meta": {"source": "biff"}}

    async def test_unattached_send_raises(self) -> None:
        with pytest.raises(ChannelUnavailableError):
            await ChannelLink().send("x", {})

    async def test_detach_closes_the_link(self) -> None:
        async with _wire() as wire:
            wire.link.detach()

            assert not wire.link.is_open
            with pytest.raises(ChannelUnavailableError):
                await wire.link.send("x", {})

    async def test_send_raises_when_the_client_stopped_reading(self) -> None:
        async with _wire() as wire:
            await wire.to_client.aclose()

            with pytest.raises(ChannelUnavailableError):
                await wire.link.send("x", {})

    async def test_send_raises_when_the_write_stream_is_closed(self) -> None:
        async with _wire() as wire:
            await wire.server_end.aclose()

            with pytest.raises(ChannelUnavailableError):
                await wire.link.send("x", {})

    async def test_dispatcher_writes_that_the_sdk_swallows_close_the_link(
        self,
    ) -> None:
        async with _wire() as wire:
            await wire.to_client.aclose()
            await wire.dispatcher.notify("notifications/x", None)

            assert not wire.link.is_open

    async def test_stalled_write_times_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(channel, "CHANNEL_SEND_TIMEOUT", 0.05)
        async with _wire(buffer=0) as wire:
            with pytest.raises(TimeoutError):
                await wire.link.send("x", {})


def _delivery_session(wire: _Wire) -> ServerSession:
    """A session whose tool-list send goes through the real dispatcher."""
    return cast(
        "ServerSession",
        SimpleNamespace(
            protocol_version="2025-11-25",
            send_tool_list_changed=wire.connection.send_tool_list_changed,
        ),
    )


async def _stall() -> None:
    await asyncio.Event().wait()


@pytest.fixture
def stalled_session() -> MagicMock:
    session = MagicMock(spec=ServerSession, protocol_version="2025-11-25")
    session.send_tool_list_changed = AsyncMock(side_effect=_stall)
    return session


@pytest.fixture(autouse=True)
def _fresh_delivery(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    _descriptions._reset_session()
    monkeypatch.setattr(_descriptions, "_NOTIFY_SEND_TIMEOUT", 0.05)
    yield
    _descriptions._reset_session()


class TestDefaultDeliveryClearsTheSession:
    """Outside channel mode DES-061 is unchanged."""

    async def test_timeout_clears_the_session(self, stalled_session: MagicMock) -> None:
        delivery = _descriptions.handshake_delivery
        await delivery.capture_session(stalled_session)
        await delivery.notify()

        assert delivery._session is None
        assert delivery._pending_notify


class TestSingleConnectionDelivery:
    """Channel mode: one connection for the process's life (DES-087)."""

    async def test_a_send_the_sdk_dropped_stays_owed(self) -> None:
        delivery = _descriptions.handshake_delivery
        async with _wire() as wire:
            delivery.serve_single_connection(wire.link)
            session = _delivery_session(wire)
            await delivery.capture_session(session)
            await wire.to_client.aclose()

            await delivery.notify()

        assert delivery._pending_notify
        assert delivery._session is session

    async def test_a_delivered_send_clears_the_debt(self) -> None:
        delivery = _descriptions.handshake_delivery
        async with _wire() as wire:
            delivery.serve_single_connection(wire.link)
            await delivery.capture_session(_delivery_session(wire))

            await delivery.notify()

        assert not delivery._pending_notify

    async def test_a_timeout_keeps_the_session(
        self, stalled_session: MagicMock
    ) -> None:
        delivery = _descriptions.handshake_delivery
        delivery.serve_single_connection(ChannelLink())
        await delivery.capture_session(stalled_session)
        await delivery.notify()

        assert delivery._session is stalled_session
        assert delivery._pending_notify

    async def test_a_code_bug_is_not_swallowed(self) -> None:
        delivery = _descriptions.handshake_delivery
        delivery.serve_single_connection(ChannelLink())
        broken = MagicMock(spec=ServerSession, protocol_version="2025-11-25")
        broken.send_tool_list_changed = AsyncMock(side_effect=RuntimeError("bug"))
        await delivery.capture_session(broken)

        with pytest.raises(RuntimeError, match="bug"):
            await delivery.notify()

    async def test_repeated_failure_logs_once_then_recovery_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        delivery = _descriptions.handshake_delivery
        link = ChannelLink()
        link.observe(anyio.create_memory_object_stream[SessionMessage](1)[0])
        delivery.serve_single_connection(link)
        flaky = MagicMock(spec=ServerSession, protocol_version="2025-11-25")
        flaky.send_tool_list_changed = AsyncMock(
            side_effect=[TimeoutError(), TimeoutError(), TimeoutError(), None, None]
        )
        with caplog.at_level(logging.INFO, logger=channel.logger.name):
            await delivery.capture_session(flaky)
            for _ in range(4):
                await delivery.notify()

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(warnings) == 1
        assert "session kept" in warnings[0].getMessage()
        assert len(infos) == 1


class TestHandshakeOnly:
    """The channel loop's request gate: refuse 2026-07-28, log the handshake."""

    @staticmethod
    def _runner(version: str = "2025-11-25") -> MagicMock:
        gate_runner = MagicMock()
        gate_runner.on_request = AsyncMock(return_value={"ok": True})
        gate_runner.connection.protocol_version = version
        return gate_runner

    @staticmethod
    def _modern() -> dict[str, object]:
        return {"_meta": {PROTOCOL_VERSION_META_KEY: "2026-07-28"}}

    async def test_initialize_logs_the_negotiated_protocol_version(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        gate: _HandshakeOnly[object] = _HandshakeOnly(self._runner())
        with caplog.at_level(logging.INFO, logger=channel_server.logger.name):
            await gate.on_request(MagicMock(), "initialize", {})

        assert "protocolVersion=2025-11-25 channel_mode=on" in caplog.text

    async def test_failed_initialize_is_logged_and_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        failing = self._runner()
        failing.on_request.side_effect = RuntimeError("no")
        with (
            caplog.at_level(logging.WARNING, logger=channel_server.logger.name),
            pytest.raises(RuntimeError),
        ):
            await _HandshakeOnly(failing).on_request(MagicMock(), "initialize", {})

        assert "initialize failed" in caplog.text

    async def test_modern_request_is_refused_without_reaching_the_runner(
        self,
    ) -> None:
        gate_runner = self._runner()

        with pytest.raises(MCPError) as refused:
            await _HandshakeOnly(gate_runner).on_request(
                MagicMock(), "tools/list", self._modern()
            )

        assert refused.value.code == INVALID_REQUEST
        gate_runner.on_request.assert_not_awaited()

    async def test_refusal_is_logged_once_with_the_method(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        gate: _HandshakeOnly[object] = _HandshakeOnly(self._runner())
        with caplog.at_level(logging.WARNING, logger=channel_server.logger.name):
            for method in ("tools/list", "tools/call"):
                with pytest.raises(MCPError):
                    await gate.on_request(MagicMock(), method, self._modern())

        assert caplog.text.count("Refused") == 1
        assert "tools/list" in caplog.text

    async def test_probe_reaches_the_runner_which_answers_method_not_found(
        self,
    ) -> None:
        gate_runner = self._runner()

        await _HandshakeOnly(gate_runner).on_request(
            MagicMock(), "server/discover", self._modern()
        )

        gate_runner.on_request.assert_awaited_once()

    async def test_handshake_era_request_passes_through(self) -> None:
        result = await _HandshakeOnly(self._runner()).on_request(
            MagicMock(), "tools/list", {}
        )

        assert result == {"ok": True}


class TestServeChannelStdio:
    """The real ``serve_channel_stdio`` over memory streams."""

    @staticmethod
    @asynccontextmanager
    async def _stdio(
        from_client: ObjectReceiveStream[SessionMessage | Exception],
        to_client: ObjectSendStream[SessionMessage],
    ) -> AsyncGenerator[tuple[object, object]]:
        yield from_client, to_client

    async def test_it_owns_a_reachable_link_and_marks_delivery_single_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        to_client_send, to_client = anyio.create_memory_object_stream[SessionMessage](
            10
        )
        to_server, from_client = anyio.create_memory_object_stream[
            SessionMessage | Exception
        ](10)
        monkeypatch.setattr(
            channel_server,
            "stdio_server",
            lambda: self._stdio(from_client, to_client_send),
        )
        server = ChannelServer[object]("biff")
        seen: list[bool] = []

        async def serve() -> None:
            await server.serve_channel_stdio()

        async with anyio.create_task_group() as tg:
            tg.start_soon(serve)
            await anyio.sleep(0.1)
            seen.append(server.channel_link.is_open)
            seen.append(_descriptions.handshake_delivery._link is server.channel_link)
            await to_server.aclose()

        assert seen == [True, True]
        assert not server.channel_link.is_open
        with pytest.raises(anyio.EndOfStream):
            to_client.receive_nowait()

    def test_the_link_is_one_object_per_server(self) -> None:
        server = ChannelServer[object]("biff")

        assert server.channel_link is server.channel_link


_SERVER_ARGS = ["mcp", "--user", "kai"]


class TestMcpEntry:
    """``biff mcp`` chooses the runner from the variable, once."""

    @pytest.fixture(autouse=True)
    def _clean_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(CHANNEL_MODE_VARIABLE, raising=False)

    @staticmethod
    def _invoke(
        channel_calls: list[str], dual_era_calls: list[Mapping[str, str]]
    ) -> int:
        async def serve_channel(_self: ChannelServer[object]) -> None:
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
        dual_era_calls: list[Mapping[str, str]] = []

        assert self._invoke(channel_calls, dual_era_calls) == 0

        assert channel_calls == ["absent"]
        assert dual_era_calls == []

    def test_without_the_variable_the_dual_era_runner_is_unchanged(self) -> None:
        channel_calls: list[str] = []
        dual_era_calls: list[Mapping[str, str]] = []

        assert self._invoke(channel_calls, dual_era_calls) == 0

        assert channel_calls == []
        assert dual_era_calls == [{"transport": "stdio"}]
