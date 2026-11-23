"""The server's poller runs no timer except connection liveness (DES-087).

Arrivals reach the poller by a relay push (NATS) or by a filesystem watch
(the local relay), never by a timer.  The one timer that remains is the
liveness reconcile that rebinds the always-on subscriptions after a client
replacement, and it exists only for a NATS-backed relay.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.client.transports import FastMCPTransport

from biff.models import BiffConfig, Message, UnreadSummary, WallPost
from biff.nats_relay import NatsRelay
from biff.server import app
from biff.server.app import create_server
from biff.server.state import ServerState
from biff.server.tools import _descriptions
from biff.server.tools._descriptions import poll_inbox
from biff.server.tools.wall import WALL_BASE_DESCRIPTION
from biff.testing.caller import StampedClient, caller_for
from tests._state import create_state

if TYPE_CHECKING:
    from fastmcp import FastMCP

_REPO = "_test-server"
_DEADLINE = 15.0  # seconds; macOS FSEvents batches a change to an existing file


@pytest.fixture
def state(tmp_path: Path) -> ServerState:
    return create_state(
        BiffConfig(user="kai", repo_name=_REPO),
        tmp_path / "relay",
        tty="tty1",
        hostname="test-host",
        pwd="/test",
        unread_path=tmp_path / "status" / "unread.json",
    )


async def _eventually(check: Callable[[], bool], what: str) -> None:
    """Wait up to ``_DEADLINE`` for *check*, failing with *what* if it never holds."""
    async with asyncio.timeout(_DEADLINE + 1.0):
        end = asyncio.get_running_loop().time() + _DEADLINE
        while not check():
            if asyncio.get_running_loop().time() > end:
                pytest.fail(f"not detected within {_DEADLINE}s: {what}")
            await asyncio.sleep(0.01)


def _unread_count(state: ServerState) -> int:
    assert state.unread_path is not None
    if not state.unread_path.exists():
        return -1
    return int(json.loads(state.unread_path.read_text())["count"])


async def _stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


class _TimerSpy:
    """Record the delay of every timer scheduled on the running loop."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.delays: list[float] = []
        loop = asyncio.get_running_loop()
        real_call_at = loop.call_at

        def call_at(
            when: float,
            callback: Callable[..., object],
            *args: object,
            context: contextvars.Context | None = None,
        ) -> asyncio.TimerHandle:
            self.delays.append(when - loop.time())
            return real_call_at(when, callback, *args, context=context)

        monkeypatch.setattr(loop, "call_at", call_at)


async def _idle(
    task: asyncio.Task[None], ready: Callable[[], bool], *, spins: int = 500
) -> None:
    """Let *task* run with no arrival, yielding without scheduling a timer.

    ``sleep(0)`` yields to the loop but never calls ``call_at``, so every
    timer the spy records was scheduled by the poller (or what it awaits).
    The poller starts its watches on a thread, so first spin until *ready*
    says its first pass has run.
    """
    while not ready():
        await asyncio.sleep(0)
    for _ in range(spins):
        await asyncio.sleep(0)
    assert not task.done(), "poller exited while idling"


class TestLocalRelayWatch:
    """The local relay's arrivals reach the poller through a filesystem watch."""

    async def test_watch_delivers_bare_user_targeted_message_and_wall(
        self, state: ServerState
    ) -> None:
        mcp = create_server(state)
        task = asyncio.create_task(poll_inbox(mcp, state))
        try:
            await _eventually(lambda: _unread_count(state) == 0, "initial refresh")

            await state.relay.deliver(
                Message(from_user="eric", to_user="kai:tty1", body="targeted")
            )
            await _eventually(lambda: _unread_count(state) == 1, "targeted message")

            await state.relay.deliver(
                Message(from_user="eric", to_user="kai", body="bare user")
            )
            await _eventually(lambda: _unread_count(state) == 2, "bare-user message")

            now = datetime.now(UTC)
            await state.relay.set_wall(
                WallPost(
                    text="deploy freeze",
                    from_user="eric",
                    posted_at=now,
                    expires_at=now + timedelta(hours=1),
                )
            )
            wall = await mcp.get_tool("wall")
            assert wall is not None
            await _eventually(lambda: "[WALL]" in (wall.description or ""), "wall post")
            assert wall.description != WALL_BASE_DESCRIPTION
        finally:
            await _stop(task)


def _nats_state(tmp_path: Path) -> ServerState:
    nc = AsyncMock()

    async def subscribe(subject: str, *, cb: object) -> AsyncMock:
        del subject, cb
        return AsyncMock()

    nc.subscribe = AsyncMock(side_effect=subscribe)
    relay = MagicMock(spec=NatsRelay)
    relay.get_nc = AsyncMock(return_value=nc)
    relay.connection_generation = 0
    relay.talk_notify_subject = MagicMock(return_value="biff.talk.notify.kai:tty1")
    relay.inbox_notify_subject = MagicMock(return_value="biff.notify.kai")
    relay.wall_notify_subject = MagicMock(return_value="biff.wallnotify")
    relay.get_wall = AsyncMock(return_value=None)
    relay.get_unread_summary = AsyncMock(return_value=UnreadSummary(count=0))
    return create_state(
        BiffConfig(user="kai", repo_name=_REPO),
        tmp_path,
        tty="tty1",
        hostname="test-host",
        pwd="/test",
        relay=relay,
    )


class TestNoTimers:
    """With no arrivals the poller schedules nothing but the liveness check."""

    async def test_local_relay_poller_schedules_only_the_watch_liveness_check(
        self, state: ServerState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spy = _TimerSpy(monkeypatch)
        mcp: FastMCP[ServerState] = create_server(state)
        task = asyncio.create_task(poll_inbox(mcp, state))
        try:
            await _idle(task, lambda: _unread_count(state) == 0)
        finally:
            await _stop(task)
        assert spy.delays
        assert {round(d) for d in spy.delays} == {
            round(_descriptions._WATCH_LIVENESS_INTERVAL)
        }

    async def test_nats_poller_schedules_only_the_liveness_reconcile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = _nats_state(tmp_path)
        relay = state.relay
        assert isinstance(relay, NatsRelay)
        spy = _TimerSpy(monkeypatch)
        mcp: FastMCP[ServerState] = create_server(state)
        task = asyncio.create_task(poll_inbox(mcp, state))
        try:
            await _idle(task, lambda: relay.get_wall.await_count > 0)  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue]
        finally:
            await _stop(task)
        assert spy.delays
        assert {round(d) for d in spy.delays} == {
            round(_descriptions._SUB_RECONCILE_INTERVAL)
        }

    async def test_a_displayed_wall_arms_one_deadline_wake_and_clearing_it_ends_it(
        self, state: ServerState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp: FastMCP[ServerState] = create_server(state)
        task = asyncio.create_task(poll_inbox(mcp, state))
        wall = await mcp.get_tool("wall")
        assert wall is not None
        try:
            await _idle(task, lambda: _unread_count(state) == 0, spins=10)
            await state.relay.set_wall(_wall_for(30))
            await _eventually(
                lambda: "[WALL]" in (wall.description or ""), "wall shown"
            )
            spy = _TimerSpy(monkeypatch)
            state.wake.wake()
            await _idle(task, lambda: True, spins=200)
            assert any(25 < d < 31 for d in spy.delays), spy.delays
            await state.relay.set_wall(None)
            await _eventually(
                lambda: wall.description == WALL_BASE_DESCRIPTION, "wall cleared"
            )
            spy.delays.clear()
            state.wake.wake()  # re-arm the wait; nothing is displayed now
            await _idle(task, lambda: True, spins=200)
            assert {round(d) for d in spy.delays} == {
                round(_descriptions._WATCH_LIVENESS_INTERVAL)
            }
        finally:
            await _stop(task)


def _wall_for(seconds: float) -> WallPost:
    now = datetime.now(UTC)
    return WallPost(
        text="deploy freeze",
        from_user="eric",
        posted_at=now,
        expires_at=now + timedelta(seconds=seconds),
    )


def _server_task_names() -> set[str]:
    """Names of the running coroutine tasks defined under ``biff/server``."""
    names: set[str] = set()
    for task in asyncio.all_tasks():
        coro = task.get_coro()
        if (
            inspect.iscoroutine(coro)
            and "/biff/server/" in coro.cr_code.co_filename
            and not task.done()
        ):
            names.add(coro.__name__)
    return names


class TestLiveSessionTimers:
    """Driving the real session startup, the only recurring work is liveness."""

    async def test_only_the_poller_and_the_heartbeat_run_in_the_background(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess")
        monkeypatch.setattr(_descriptions, "_WATCH_LIVENESS_INTERVAL", 0.3)
        monkeypatch.setitem(app._heartbeat_loop.__kwdefaults__ or {}, "interval", 0.3)
        state = create_state(
            BiffConfig(user="kai", repo_name=_REPO),
            tmp_path / "relay",
            hostname="h",
            pwd="/p",
            unread_dir=tmp_path / "unread",
        )
        spy = _TimerSpy(monkeypatch)
        loop = asyncio.get_running_loop()
        async with StampedClient(
            FastMCPTransport(create_server(state)), caller=caller_for("sess")
        ):
            # Spin, never sleep: the test must add no timer of its own.
            end = loop.time() + 2.5
            while loop.time() < end:
                await asyncio.sleep(0)
            background = _server_task_names()
        # No sentinel reaper (it ran every 2s) and no other loop.
        assert background == {"poll_inbox", "_heartbeat_loop"}
        # Whatever recurs is the 0.3s liveness check and heartbeat; the MCP
        # transport's own one-shot request timeouts (10s, 5s, 3s) do not repeat.
        repeats = [d for d in spy.delays if d < 1]
        assert repeats
        assert {round(d, 1) for d in repeats} == {0.3}
