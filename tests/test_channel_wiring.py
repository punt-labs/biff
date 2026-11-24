"""The production paths that reach the channel emitter, with a real emitter.

Each test drives a real call site and asserts that the emitter acted, so deleting
the call leaves a test red (``docs/notification.tex``, "What the implementation
owes").
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from biff.models import BiffConfig, Message, UserSession, WallPost
from biff.nats_relay import NatsRelay
from biff.server import channel
from biff.server.app import create_server
from biff.server.channel import ChannelLink
from biff.server.channel_handshake import ChannelHandshake
from biff.server.state import ServerState
from biff.server.tools import _descriptions
from biff.server.tools._activity import track_activity
from biff.server.tools._descriptions import (
    _InboxPokeGate,
    _reconcile_always_on_sub,
    poll_inbox,
    subscribe_talk,
)
from biff.talk_latch import TalkNotifyLatch
from tests._state import create_state
from tests.test_channel_emitter import _Sink

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

_DEADLINE = 15.0


class _FakeNc:
    """A NATS client that keeps the callback a subscription was given."""

    def __init__(self) -> None:
        self.callback: Callable[[object], Awaitable[None]] | None = None

    async def subscribe(
        self, subject: str, cb: Callable[[object], Awaitable[None]]
    ) -> MagicMock:
        del subject
        self.callback = cb
        return MagicMock(unsubscribe=AsyncMock())


class _FakeNats(NatsRelay):
    """A NATS relay that never dials: it hands out a recording client."""

    nc = _FakeNc()

    async def get_nc(self) -> _FakeNc:  # type: ignore[override]
        return self.nc

    async def get_session(self, session_key: str) -> UserSession | None:
        del session_key
        return None

    async def inbox_high_water(self, session_key: str) -> int:
        del session_key
        return 0

    async def get_wall(self, *, repo: str | None = None) -> WallPost | None:
        del repo
        return None


@dataclass
class _Wired:
    state: ServerState
    sink: _Sink
    link: ChannelLink


@pytest.fixture(autouse=True)
def _short_send_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(channel, "CHANNEL_SEND_TIMEOUT", 0.05)


def _wire(state: ServerState) -> _Wired:
    sink = _Sink()
    link = ChannelLink()
    link.observe(sink)
    state.emitter.serve(link)
    state.emitter.initialized()
    return _Wired(state, sink, link)


@pytest.fixture
def wired(tmp_path: Path) -> _Wired:
    state = create_state(
        BiffConfig(user="kai", repo_name="_test-wiring"),
        tmp_path / "relay",
        tty="tty1",
        hostname="test-host",
        pwd="/test",
    )
    return _wire(state)


async def _until(check: Callable[[], bool], what: str) -> None:
    try:
        async with asyncio.timeout(_DEADLINE):
            while not check():
                await asyncio.sleep(0.02)
    except TimeoutError:
        pytest.fail(f"timed out waiting for {what}")


async def _make_dormant_mail(w: _Wired) -> None:
    """Leave mail owed with its kind asleep after two timed-out sends."""
    await w.state.relay.deliver(
        Message(from_user="eric", to_user=w.state.session_key, body="hi")
    )
    w.sink.stall = True
    await w.state.emitter.check()
    await asyncio.sleep(1.1)  # RETRY_SECONDS: the deferred send times out again
    await asyncio.sleep(0.3)
    w.sink.stall = False
    assert w.state.emitter.held == frozenset()
    assert w.sink.frames == []


class TestPollerPass:
    async def test_a_poller_pass_checks_the_emitter(self, wired: _Wired) -> None:
        """``poll_inbox`` calls ``state.emitter.check()`` after a pass."""
        state = wired.state
        mcp = create_server(state)
        await state.relay.deliver(
            Message(from_user="eric", to_user=state.session_key, body="hi")
        )
        task = asyncio.create_task(poll_inbox(mcp, state))
        try:
            await _until(lambda: bool(wired.sink.frames), "a mail event")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert wired.sink.metas == [{"kind": "mail", "count": "1"}]


class TestTalkSubscription:
    async def test_a_talk_frame_is_counted_by_the_emitter(self, tmp_path: Path) -> None:
        """The talk subscription feeds ``receive_frame``, so an invite is announced."""
        relay = _FakeNats(url="nats://127.0.0.1:1", repo_name="_test-wiring")
        state = create_state(
            BiffConfig(user="kai", repo_name="_test-wiring"),
            tmp_path / "relay",
            relay=relay,
            tty="tty1",
            hostname="h",
            pwd="/",
        )
        w = _wire(state)
        binding = await subscribe_talk(
            state,
            TalkNotifyLatch.for_resubscribe(_descriptions.logger),
            _InboxPokeGate(),
        )
        assert binding is not None
        callback = relay.nc.callback
        assert callback is not None

        frame = {
            "type": "invite",
            "from": "eric",
            "from_tty": "tty9",
            "from_key": "eric:def",
            "to_key": state.session_key,
            "body": "hi",
        }
        await callback(MagicMock(data=json.dumps(frame).encode()))
        await state.emitter.check()

        assert w.sink.metas == [{"kind": "talk_invite"}]


class TestRebind:
    async def test_a_subscription_rebind_frees_a_dormant_kind(
        self, wired: _Wired
    ) -> None:
        """``_reconcile_always_on_sub`` calls ``emitter.reconnected()``."""
        await _make_dormant_mail(wired)
        await wired.state.emitter.check()
        assert wired.sink.frames == []  # nothing has woken it

        await _reconcile_always_on_sub(
            None,
            1,
            lambda: _binding(),
            state=wired.state,
            gate=_InboxPokeGate(),
        )
        await wired.state.emitter.check()

        assert len(wired.sink.frames) == 1
        assert wired.state.wake.is_set


async def _binding() -> _descriptions.SubscriptionBinding:
    return _descriptions.SubscriptionBinding(MagicMock(unsubscribe=AsyncMock()), 1)


class TestToolCall:
    async def test_a_tool_call_frees_a_dormant_kind(self, wired: _Wired) -> None:
        """``track_activity`` calls ``emitter.tool_called()`` after the tool."""
        await _make_dormant_mail(wired)
        wired.state.wake.wake()  # leave a known state
        calls: list[str] = []

        @track_activity(wired.state)
        async def tool() -> str:
            calls.append("ran")
            return "ok"

        assert await tool() == "ok"
        await wired.state.emitter.check()

        assert calls == ["ran"]
        assert len(wired.sink.frames) == 1


class TestSecondInitializeWithADebt:
    async def test_a_second_initialize_holds_events_until_initialized(
        self, wired: _Wired
    ) -> None:
        """O1: ``initialize`` again, session captured, a tool-list change owed."""
        state = wired.state
        session = MagicMock(
            protocol_version="2025-06-18", send_tool_list_changed=AsyncMock()
        )
        await _descriptions.handshake_delivery.capture_session(session)
        _descriptions.handshake_delivery._pending_notify = True
        await state.relay.deliver(
            Message(from_user="eric", to_user=state.session_key, body="hi")
        )
        on_request, on_notify = AsyncMock(return_value={}), AsyncMock()
        request, notify = ChannelHandshake.watch(state.emitter, on_request, on_notify)

        await request(MagicMock(), "initialize", {})
        await _descriptions.handshake_delivery.capture_session(session)
        await state.emitter.check()
        assert wired.sink.frames == []

        await notify(MagicMock(), "notifications/initialized", None)
        await state.emitter.check()
        assert len(wired.sink.frames) == 1


class TestWatchOrder:
    async def test_initialize_closes_the_gate_before_the_runner_answers(self) -> None:
        order: list[str] = []
        emitter = MagicMock()
        emitter.initializing.side_effect = lambda: order.append("gate")

        async def runner(*_args: object) -> dict[str, object]:
            order.append("runner")
            return {}

        on_request = AsyncMock(side_effect=runner)
        request, _ = ChannelHandshake.watch(emitter, on_request, AsyncMock())

        await request(MagicMock(), "initialize", {})

        assert order == ["gate", "runner"]

    async def test_initialized_opens_the_gate_after_the_runner_has_it(self) -> None:
        order: list[str] = []
        emitter = MagicMock()
        emitter.initialized.side_effect = lambda: order.append("gate")

        async def runner(*_args: object) -> None:
            order.append("runner")

        on_notify = AsyncMock(side_effect=runner)
        _, notify = ChannelHandshake.watch(emitter, AsyncMock(), on_notify)

        await notify(MagicMock(), "notifications/initialized", None)

        assert order == ["runner", "gate"]
