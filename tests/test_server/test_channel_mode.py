"""Channel mode for the Claude Code plugin (DES-087): the unit-level contract.

Streams here are the SDK's real ones (a ``JSONRPCDispatcher`` over memory
streams), because the SDK swallows a write onto a closed stream and a fake
that raises would prove nothing.  The wire behavior through the real
``biff mcp`` process lives in ``tests/test_subprocess/test_channel_mode.py``.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import anyio
import pytest
from mcp.server.connection import Connection
from mcp.server.session import ServerSession
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from mcp.shared.message import SessionMessage
from mcp.types import (
    INVALID_REQUEST,
    PROTOCOL_VERSION_META_KEY,
    JSONRPCNotification,
    JSONRPCRequest,
)
from typer.testing import CliRunner

from biff.__main__ import app
from biff.models import Message
from biff.server import channel, channel_server, handshake_delivery
from biff.server.app import _SessionCaptureMiddleware, create_server
from biff.server.channel import (
    CHANNEL_METHOD,
    CHANNEL_MODE_VARIABLE,
    ChannelLink,
    ChannelUnavailableError,
    take_channel_mode,
)
from biff.server.channel_server import (
    ChannelServer,
    _HandshakeOnly,
)
from biff.server.state import ServerState
from biff.server.tools import _descriptions
from biff.server.tools._descriptions import (
    _InboxPokeGate,
    poll_inbox,
    refresh_read_messages,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from anyio import AsyncFile
    from anyio.abc import ObjectReceiveStream, ObjectSendStream
    from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
    from fastmcp import Context
    from mcp.server.subscriptions import SubscriptionBus
    from mcp.shared.transport_context import TransportContext

runner = CliRunner()

_KAI_SESSION = "kai:tty1"


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
    monkeypatch.setattr(handshake_delivery, "_NOTIFY_SEND_TIMEOUT", 0.05)
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

    async def test_a_code_bug_is_logged_and_stays_owed_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        delivery = _descriptions.handshake_delivery
        delivery.serve_single_connection(ChannelLink())
        broken = MagicMock(spec=ServerSession, protocol_version="2025-11-25")
        broken.send_tool_list_changed = AsyncMock(side_effect=RuntimeError("bug"))
        await delivery.capture_session(broken)
        with caplog.at_level(logging.WARNING):
            await delivery.notify()

        assert delivery._pending_notify
        assert delivery._session is broken
        assert [r.exc_info is not None for r in caplog.records] == [True]

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
        infos = [
            r
            for r in caplog.records
            if r.levelno == logging.INFO and r.name == channel.logger.name
        ]
        assert len(warnings) == 2
        assert "Tool list change (session kept)" in warnings[0].getMessage()
        assert "TimeoutError" in warnings[0].getMessage()
        assert "recovered" in warnings[1].getMessage()
        assert infos == []


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
            for method in ("tools/list", "tools/list"):
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


@dataclass(frozen=True, slots=True)
class _Client:
    """The client end of a served ``ChannelServer`` over memory streams."""

    server: ChannelServer[object]
    to_server: MemoryObjectSendStream[SessionMessage | Exception]
    from_server: MemoryObjectReceiveStream[SessionMessage]

    async def send(self, message: JSONRPCRequest | JSONRPCNotification) -> None:
        await self.to_server.send(SessionMessage(message))

    async def call(self, request_id: int, name: str) -> None:
        await self.send(
            JSONRPCRequest(
                jsonrpc="2.0",
                id=request_id,
                method="tools/call",
                params={"name": name, "arguments": {}},
            )
        )

    def received_methods(self) -> list[str]:
        """Every notification the server wrote, without waiting."""
        methods: list[str] = []
        while True:
            try:
                frame = self.from_server.receive_nowait().message
            except (anyio.WouldBlock, anyio.EndOfStream):
                return methods
            if isinstance(frame, JSONRPCNotification):
                methods.append(frame.method)


@asynccontextmanager
async def _served(
    monkeypatch: pytest.MonkeyPatch,
    register: Callable[[ChannelServer[object]], None],
) -> AsyncGenerator[_Client]:
    """Run the real ``serve_channel_stdio`` with session capture, handshaken."""
    server_end, from_server = anyio.create_memory_object_stream[SessionMessage](10)
    to_server, from_client = anyio.create_memory_object_stream[
        SessionMessage | Exception
    ](10)

    @asynccontextmanager
    async def stdio() -> AsyncGenerator[tuple[object, object]]:
        yield from_client, server_end

    monkeypatch.setattr(channel_server, "stdio_server", stdio)
    server = ChannelServer[object]("biff")
    server.add_middleware(_SessionCaptureMiddleware())
    register(server)
    client = _Client(server, to_server, from_server)
    async with anyio.create_task_group() as tg:
        tg.start_soon(server.serve_channel_stdio)
        await anyio.sleep(0.05)
        await client.send(
            JSONRPCRequest(
                jsonrpc="2.0",
                id=1,
                method="initialize",
                params={
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            )
        )
        await from_server.receive()
        await client.send(
            JSONRPCNotification(jsonrpc="2.0", method="notifications/initialized")
        )
        await anyio.sleep(0.05)
        yield client
        tg.cancel_scope.cancel()


class TestInRequestNotify:
    """A tool call that changes a description notifies inside its request."""

    @staticmethod
    def _register(server: ChannelServer[object]) -> None:
        @server.tool
        async def poke() -> str:
            await _descriptions.handshake_delivery.notify()
            return "ok"

    async def test_a_delivered_notification_clears_the_debt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with _served(monkeypatch, self._register) as client:
            await client.call(2, "poke")
            await anyio.sleep(0.1)

            assert "notifications/tools/list_changed" in client.received_methods()
        assert not _descriptions.handshake_delivery._pending_notify

    async def test_a_dropped_notification_stays_owed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with _served(monkeypatch, self._register) as client:
            await client.from_server.aclose()
            await client.call(2, "poke")
            await anyio.sleep(0.1)

            assert _descriptions.handshake_delivery._pending_notify
            assert not client.server.channel_link.is_open


class TestTeardownWindow:
    """After client EOF, while a handler is still finishing, nothing is delivered."""

    @staticmethod
    def _register(server: ChannelServer[object]) -> None:
        @server.tool
        async def slow() -> str:
            try:
                await anyio.sleep(10)
            finally:
                with anyio.CancelScope(shield=True):
                    await anyio.sleep(0.3)
            return "slow"

    async def test_eof_closes_the_link_and_a_notify_stays_owed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with _served(monkeypatch, self._register) as client:
            await client.call(3, "slow")
            await anyio.sleep(0.05)
            await client.to_server.aclose()
            await anyio.sleep(0.05)

            assert not client.server.channel_link.is_open
            await _descriptions.handshake_delivery.notify()

            assert _descriptions.handshake_delivery._pending_notify


class TestOwedDebtIsRetried:
    """A timed-out send is retried by the poller, not only by the next change."""

    @pytest.fixture(autouse=True)
    def _no_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(channel, "CHANNEL_BACKOFF", 0.0)

    @staticmethod
    def _flaky() -> MagicMock:
        session = MagicMock(spec=ServerSession, protocol_version="2025-11-25")
        session.send_tool_list_changed = AsyncMock(side_effect=[TimeoutError(), None])
        return session

    @staticmethod
    def _single_connection() -> ChannelLink:
        link = ChannelLink()
        link.observe(anyio.create_memory_object_stream[SessionMessage](1)[0])
        _descriptions.handshake_delivery.serve_single_connection(link)
        return link

    async def test_flush_owed_delivers_after_a_timeout(self) -> None:
        delivery = _descriptions.handshake_delivery
        self._single_connection()
        session = self._flaky()
        await delivery.capture_session(session)
        await delivery.notify()
        was_owed = delivery._pending_notify
        await delivery.flush_owed()

        assert was_owed

        assert not delivery._pending_notify
        assert session.send_tool_list_changed.await_count == 2

    async def test_flush_owed_is_idle_when_nothing_is_owed(self) -> None:
        delivery = _descriptions.handshake_delivery
        self._single_connection()
        session = MagicMock(spec=ServerSession, protocol_version="2025-11-25")
        session.send_tool_list_changed = AsyncMock()
        await delivery.capture_session(session)

        await delivery.flush_owed()

        session.send_tool_list_changed.assert_not_awaited()

    async def test_flush_owed_does_nothing_outside_channel_mode(self) -> None:
        delivery = _descriptions.handshake_delivery
        session = self._flaky()
        await delivery.capture_session(session)
        delivery._pending_notify = True

        await delivery.flush_owed()

        session.send_tool_list_changed.assert_not_awaited()


class TestSessionCapture:
    """A capture that does not happen is not silent."""

    @staticmethod
    def _context(*, session_error: bool) -> MagicMock:
        context = MagicMock()
        if session_error:
            type(context.fastmcp_context).session = PropertyMock(
                side_effect=RuntimeError("no session")
            )
        return context

    async def test_a_missing_session_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        middleware = _SessionCaptureMiddleware()
        with caplog.at_level(logging.WARNING, logger=channel_server.logger.name):
            await middleware.on_initialize(
                self._context(session_error=True), AsyncMock(return_value=None)
            )

        assert "not captured" in caplog.text

    async def test_a_failed_flush_does_not_fail_the_handshake(self) -> None:
        context = self._context(session_error=False)
        context.fastmcp_context.session.protocol_version = "2025-11-25"
        context.fastmcp_context.session.send_tool_list_changed = AsyncMock(
            side_effect=RuntimeError("bug")
        )
        _descriptions.handshake_delivery.serve_single_connection(ChannelLink())
        _descriptions.handshake_delivery._pending_notify = True

        await _SessionCaptureMiddleware().on_initialize(
            context, AsyncMock(return_value=None)
        )

        assert _descriptions.handshake_delivery._session is not None
        assert _descriptions.handshake_delivery._pending_notify


class TestFailureLogging:
    """Failures name their cause; the latch wording is neutral."""

    def test_a_channel_failure_does_not_read_as_a_tool_list_failure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        link = ChannelLink()
        with caplog.at_level(logging.INFO, logger=channel.logger.name):
            link.failed("Channel event", TimeoutError("slow"))
            link.failed("Channel event", TimeoutError("slow"))
            link.delivered("Channel event")

        assert "Tool list" not in caplog.text
        assert caplog.text.count("Channel event failed") == 1
        assert "TimeoutError('slow')" in caplog.text
        assert caplog.text.count("Channel event recovered") == 1

    async def test_default_mode_logs_the_cause(
        self, caplog: pytest.LogCaptureFixture, stalled_session: MagicMock
    ) -> None:
        delivery = _descriptions.handshake_delivery
        await delivery.capture_session(stalled_session)
        with caplog.at_level(logging.WARNING):
            await delivery.notify()

        assert [r.exc_info is not None for r in caplog.records] == [True]

    async def test_default_mode_logs_a_code_bug_and_does_not_raise(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        delivery = _descriptions.handshake_delivery
        broken = MagicMock(spec=ServerSession, protocol_version="2025-11-25")
        broken.send_tool_list_changed = AsyncMock(side_effect=RuntimeError("bug"))
        await delivery.capture_session(broken)
        with caplog.at_level(logging.WARNING):
            await delivery.notify()

        assert delivery._session is None
        assert delivery._pending_notify
        assert [r.exc_info is not None for r in caplog.records] == [True]

    async def test_malformed_input_names_the_error_truncated(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=channel_server.logger.name):
            await channel_server._log_malformed_input(ValueError("x" * 1000))

        assert "ValueError" in caplog.text
        assert "x" * 1000 not in caplog.text
        assert "x" * 50 in caplog.text

    async def test_each_distinct_refused_method_is_logged_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        gate: _HandshakeOnly[object] = _HandshakeOnly(MagicMock())
        params = {"_meta": {PROTOCOL_VERSION_META_KEY: "2026-07-28"}}
        with caplog.at_level(logging.WARNING, logger=channel_server.logger.name):
            for method in ("tools/list", "tools/call", "tools/list", "tools/call"):
                with pytest.raises(MCPError):
                    await gate.on_request(MagicMock(), method, params)

        assert caplog.text.count("Refused") == 2


class TestShutdown:
    async def test_the_write_stream_closes_even_if_connection_cleanup_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def broken(_connection: object) -> None:
            msg = "cleanup failed"
            raise RuntimeError(msg)

        monkeypatch.setattr(channel_server, "aclose_shielded", broken)
        server_end, from_server = anyio.create_memory_object_stream[SessionMessage](1)
        to_server, from_client = anyio.create_memory_object_stream[
            SessionMessage | Exception
        ](1)

        @asynccontextmanager
        async def stdio() -> AsyncGenerator[tuple[object, object]]:
            yield from_client, server_end

        monkeypatch.setattr(channel_server, "stdio_server", stdio)
        await to_server.aclose()

        with pytest.raises(RuntimeError, match="cleanup failed"):
            await ChannelServer[object]("biff").serve_channel_stdio()

        with pytest.raises(anyio.EndOfStream):
            from_server.receive_nowait()


def _state_with_counting_relay(state: ServerState) -> tuple[AsyncMock, AsyncMock]:
    unread = AsyncMock(return_value=MagicMock(count=0))
    wall = AsyncMock(return_value=None)
    state.relay.get_unread_summary = unread  # type: ignore[method-assign]
    state.relay.get_wall = wall  # type: ignore[method-assign]
    return unread, wall


class TestAFailingFlushDoesNotSkipTheTick:
    async def test_the_rest_of_the_iteration_runs_and_the_bug_is_logged_once(
        self,
        state: ServerState,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        delivery = _descriptions.handshake_delivery
        link = ChannelLink()
        link.observe(anyio.create_memory_object_stream[SessionMessage](1)[0])
        delivery.serve_single_connection(link)
        broken = MagicMock(spec=ServerSession, protocol_version="2025-11-25")
        broken.send_tool_list_changed = AsyncMock(side_effect=TypeError("bug"))
        await delivery.capture_session(broken)
        delivery._pending_notify = True
        monkeypatch.setattr(channel, "CHANNEL_BACKOFF", 0.0)
        unread, wall = _state_with_counting_relay(state)

        with caplog.at_level(logging.WARNING):
            task = asyncio.create_task(poll_inbox(create_server(state), state))
            for _ in range(8):  # arrivals wake the poller; no timer does
                state.wake.wake()
                await asyncio.sleep(0.05)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        assert unread.await_count >= 3
        assert wall.await_count >= 3
        assert [r.levelno for r in caplog.records] == [logging.ERROR]

    async def test_an_unexpected_tick_failure_is_a_latched_warning(
        self,
        state: ServerState,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        async def broken(*_args: object, **_kwargs: object) -> None:
            msg = "tick bug"
            raise RuntimeError(msg)

        monkeypatch.setattr(_descriptions, "_active_tick", broken)
        gate = _InboxPokeGate()
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                await _descriptions._safe_tick(
                    create_server(state), state, _descriptions._Seen(), gate=gate
                )

        assert [r.levelno for r in caplog.records] == [logging.ERROR]


class TestRefreshNeverFailsTheCallersResult:
    async def test_a_code_bug_in_the_notify_does_not_fail_the_refresh(
        self, state: ServerState
    ) -> None:
        await state.relay.deliver(
            Message(from_user="eric", to_user=_KAI_SESSION, body="hi")
        )
        delivery = _descriptions.handshake_delivery
        broken = MagicMock(spec=ServerSession, protocol_version="2025-11-25")
        broken.send_tool_list_changed = AsyncMock(side_effect=TypeError("bug"))
        await delivery.capture_session(broken)

        assert await refresh_read_messages(create_server(state), state)
        assert delivery._pending_notify


class TestWedgedClient:
    async def test_the_timeout_warning_is_not_empty(
        self, caplog: pytest.LogCaptureFixture, stalled_session: MagicMock
    ) -> None:
        delivery = _descriptions.handshake_delivery
        delivery.serve_single_connection(ChannelLink())
        await delivery.capture_session(stalled_session)
        with caplog.at_level(logging.WARNING):
            await delivery.notify()

        assert "TimeoutError()" in caplog.text

    async def test_flush_backs_off_after_a_timeout(
        self, monkeypatch: pytest.MonkeyPatch, stalled_session: MagicMock
    ) -> None:
        delivery = _descriptions.handshake_delivery
        delivery.serve_single_connection(ChannelLink())
        now = [100.0]
        monkeypatch.setattr(channel, "time", SimpleNamespace(monotonic=lambda: now[0]))
        await delivery.capture_session(stalled_session)
        await delivery.notify()

        await delivery.flush_owed()
        assert stalled_session.send_tool_list_changed.await_count == 1

        now[0] += channel.CHANNEL_BACKOFF + 1
        await delivery.flush_owed()
        assert stalled_session.send_tool_list_changed.await_count == 2


class TestTransportCrash:
    async def test_a_stdin_failure_is_logged_before_it_propagates(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        class Stdin:
            def __aiter__(self) -> Stdin:
                return self

            async def __anext__(self) -> str:
                await anyio.sleep(0.05)
                raise OSError(5, "Input/output error")

        class Stdout:
            async def write(self, text: str) -> int:
                return len(text)

            async def flush(self) -> None:
                return None

        monkeypatch.setattr(
            channel_server,
            "stdio_server",
            partial(
                stdio_server,
                stdin=cast("AsyncFile[str]", Stdin()),
                stdout=cast("AsyncFile[str]", Stdout()),
            ),
        )
        with (
            caplog.at_level(logging.ERROR, logger=channel_server.logger.name),
            pytest.raises(Exception),  # noqa: B017
        ):
            await ChannelServer[object]("biff").serve_channel_stdio()

        assert "channel stdio transport failed" in caplog.text


class TestACancelledInRequestSendKeepsTheDebt:
    async def test_cancelling_notify_mid_send_leaves_it_owed(self) -> None:
        from fastmcp.server.context import _current_context

        delivery = _descriptions.handshake_delivery
        link = ChannelLink()
        link.observe(anyio.create_memory_object_stream[SessionMessage](1)[0])
        delivery.serve_single_connection(link)
        sending = asyncio.Event()

        class _HangingContext:
            session = SimpleNamespace(protocol_version="2025-11-25")

            async def send_notification(self, notification: object) -> None:
                sending.set()
                await asyncio.Event().wait()

        context = contextvars.copy_context()
        context.run(_current_context.set, cast("Context", _HangingContext()))
        task = asyncio.create_task(delivery.notify(), context=context)
        await sending.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert delivery._pending_notify


class TestOwedDebtIsRetriedOnEveryWake:
    async def test_a_timeout_wake_with_no_tick_still_flushes(
        self, state: ServerState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        delivery = _descriptions.handshake_delivery
        link = ChannelLink()
        link.observe(anyio.create_memory_object_stream[SessionMessage](1)[0])
        delivery.serve_single_connection(link)
        session = MagicMock(spec=ServerSession, protocol_version="2025-11-25")
        session.send_tool_list_changed = AsyncMock()
        await delivery.capture_session(session)
        monkeypatch.setattr(channel, "CHANNEL_BACKOFF", 0.6)

        async def settle(
            _mcp: object,
            _state: object,
            seen: _descriptions._Seen,
            *,
            gate: _InboxPokeGate,
            due: bool = False,
        ) -> _descriptions._Seen:
            gate.claim()  # a pass that succeeded
            return seen

        ticks = AsyncMock(side_effect=settle)
        monkeypatch.setattr(_descriptions, "_safe_tick", ticks)
        task = asyncio.create_task(poll_inbox(create_server(state), state))
        await asyncio.sleep(0.3)  # the first pass is over; nothing else will wake it
        link.failed("send", TimeoutError())  # back-off starts
        delivery._pending_notify = True
        state.wake.wake()  # as a tool call that leaves a debt does
        await asyncio.sleep(0.2)  # that wake's own pass runs
        assert delivery.seconds_until_retry() is not None  # inside the back-off
        before = ticks.await_count
        await asyncio.sleep(0.8)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        assert delivery.seconds_until_retry() is None  # the debt is paid
        assert ticks.await_count == before  # the back-off's wake ran no tick


class TestNoSessionCapturedIsAFailureEpisode:
    @staticmethod
    def _owed_link(monkeypatch: pytest.MonkeyPatch) -> list[float]:
        from biff import failure_log

        delivery = _descriptions.handshake_delivery
        now = [1000.0]
        clock = SimpleNamespace(monotonic=lambda: now[0])
        monkeypatch.setattr(failure_log, "time", clock)
        monkeypatch.setattr(channel, "time", clock)
        monkeypatch.setattr(channel, "CHANNEL_BACKOFF", 0.0)
        link = ChannelLink()
        link.observe(anyio.create_memory_object_stream[SessionMessage](1)[0])
        delivery.serve_single_connection(link)
        delivery._pending_notify = True
        return now

    async def test_nothing_is_logged_inside_the_handshake_window(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        delivery = _descriptions.handshake_delivery
        self._owed_link(monkeypatch)
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                await delivery.flush_owed()

        assert caplog.records == []
        assert delivery._pending_notify

    async def test_after_the_window_it_warns_then_reminds_and_recovers(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from biff import failure_log

        delivery = _descriptions.handshake_delivery
        now = self._owed_link(monkeypatch)
        now[0] += channel.HANDSHAKE_WINDOW + 1
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                await delivery.flush_owed()
            now[0] += failure_log.REMINDER_SECONDS
            await delivery.flush_owed()
            session = MagicMock(spec=ServerSession, protocol_version="2025-11-25")
            session.send_tool_list_changed = AsyncMock()
            await delivery.capture_session(session)

        messages = [r.getMessage() for r in caplog.records]
        assert "no session captured" in messages[0]
        assert "still failing" in messages[1]
        assert "recovered" in messages[2]
        assert not delivery._pending_notify


class TestTheFlagDoesNotLeakIntoConfigLoadSubprocesses:
    def test_a_subprocess_started_while_loading_config_does_not_see_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess
        import sys

        monkeypatch.setenv(CHANNEL_MODE_VARIABLE, "1")
        seen: list[str] = []

        def load_config(**_kwargs: object) -> MagicMock:
            probe = "import os; print(os.environ.get('BIFF_CHANNEL_MODE'))"
            out = subprocess.run(  # noqa: S603
                [sys.executable, "-c", probe],
                capture_output=True,
                text=True,
                check=True,
            )
            seen.append(out.stdout.strip())
            return MagicMock()

        async def serve_channel(_self: ChannelServer[object]) -> None:
            return None

        with (
            patch(
                "biff.server.transport_runtime.create_server",
                return_value=ChannelServer[object]("biff"),
            ),
            patch("biff.server.launch.create_state"),
            patch("biff.server.entrypoints.load_mcp_config", side_effect=load_config),
            patch.object(ChannelServer, "serve_channel_stdio", serve_channel),
        ):
            assert runner.invoke(app, _SERVER_ARGS).exit_code == 0

        assert seen == ["None"]


class TestACancelAtTheBusCheckpointKeepsTheDebt:
    async def test_cancelling_notify_while_publishing_leaves_it_owed(self) -> None:
        delivery = _descriptions.handshake_delivery
        publishing = asyncio.Event()

        class _HangingBus:
            async def publish(self, event: object) -> None:
                publishing.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(
            delivery.notify(cast("SubscriptionBus", _HangingBus()))
        )
        await publishing.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert delivery._pending_notify


class TestAStartupNotifyBeforeTheStreamAttaches:
    async def test_it_is_inside_the_handshake_window(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        delivery = _descriptions.handshake_delivery
        monkeypatch.setattr(channel, "CHANNEL_BACKOFF", 0.0)
        delivery.serve_single_connection(ChannelLink())
        delivery._pending_notify = True
        with caplog.at_level(logging.WARNING):
            await delivery.flush_owed()

        assert caplog.records == []
        assert delivery._pending_notify
