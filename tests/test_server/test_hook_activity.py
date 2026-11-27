"""A session's idle time must follow tool use that never calls a biff tool.

``last_tool_at`` used to advance only inside biff's own tool handlers.  An
agent working through Bash, Edit and the like for hours read as idle to every
other viewer, while its own ``/who`` showed 0m because ``who`` itself is a biff
call.  The plugin's PostToolUse hook now stamps ``<session_id>.active`` on every
tool call, and the heartbeat loop carries that time into the shared row.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from biff.models import BiffConfig, UserSession
from biff.relay import LocalRelay
from biff.server.app import _heartbeat_loop
from biff.server.state import ServerState
from biff.server.tools._session import update_current_session
from biff.session_routing import SessionRouting
from tests._state import create_state

_SESSION_ID = "sess-pl1694"
_TEN_MINUTES = timedelta(minutes=10)


async def _owner_with_stale_row(tmp_path: Path) -> ServerState:
    """Register a session whose shared row says its last tool call was 10m ago."""
    state = create_state(
        BiffConfig(user="kai", repo_name="r"),
        tmp_path / "relay",
        hostname="keble",
        pwd="/p",
        unread_dir=tmp_path / "unread",
    )
    state.bind(SessionRouting(_SESSION_ID))
    await update_current_session(state)
    row = await state.relay.get_session(state.session_key)
    assert row is not None
    stale = datetime.now(UTC) - _TEN_MINUTES
    await state.relay.update_session(row.model_copy(update={"last_tool_at": stale}))
    return state


def _stamp(tmp_path: Path, when: datetime) -> Path:
    """Write the file the PostToolUse hook touches, dated *when*."""
    path = SessionRouting(_SESSION_ID).unread_path(tmp_path / "unread")
    path = path.with_suffix(".active")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    os.utime(path, (when.timestamp(), when.timestamp()))
    return path


async def _run_heartbeats(state: ServerState, seconds: float) -> None:
    stop = asyncio.Event()
    loop = asyncio.create_task(_heartbeat_loop(state, stop, interval=0.01))
    await asyncio.sleep(seconds)
    stop.set()
    await loop


async def _idle_seen_by_another_host(tmp_path: Path, key: str) -> timedelta:
    viewer = LocalRelay(tmp_path / "relay")  # a second process on the same relay
    row = await viewer.get_session(key)
    assert row is not None
    return datetime.now(UTC) - row.last_tool_at


class _CountsRowRewrites(LocalRelay):
    """A relay that counts the whole-row rewrites a heartbeat tick makes.

    A rewrite built from an earlier read can land after a concurrent ``/plan``
    and put the old plan back, so a tick must not make one of its own.
    """

    rewrites = 0

    async def update_session(self, session: UserSession) -> None:
        self.rewrites += 1
        await super().update_session(session)


class TestCarryingTheStampLosesNoConcurrentWrite:
    async def test_the_tick_makes_no_row_rewrite_of_its_own(
        self, tmp_path: Path
    ) -> None:
        relay = _CountsRowRewrites(tmp_path / "relay")
        state = create_state(
            BiffConfig(user="kai", repo_name="r"),
            tmp_path / "relay",
            relay=relay,
            hostname="keble",
            pwd="/p",
            unread_dir=tmp_path / "unread",
        )
        state.bind(SessionRouting(_SESSION_ID))
        await update_current_session(state, plan="Investigating")
        before = relay.rewrites
        _stamp(tmp_path, datetime.now(UTC) + timedelta(seconds=5))
        await state.heartbeat()
        row = await LocalRelay(tmp_path / "relay").get_session(state.session_key)
        assert row is not None
        assert (relay.rewrites - before, row.plan) == (0, "Investigating")
        assert row.last_tool_at > datetime.now(UTC)


class TestHookObservedToolUse:
    async def test_without_a_stamp_the_viewer_sees_the_session_idle(
        self, tmp_path: Path
    ) -> None:
        state = await _owner_with_stale_row(tmp_path)
        await _run_heartbeats(state, 0.1)
        idle = await _idle_seen_by_another_host(tmp_path, state.session_key)
        assert idle >= _TEN_MINUTES

    async def test_a_hook_stamp_makes_the_viewer_see_the_session_fresh(
        self, tmp_path: Path
    ) -> None:
        state = await _owner_with_stale_row(tmp_path)
        _stamp(tmp_path, datetime.now(UTC))
        await _run_heartbeats(state, 0.1)
        idle = await _idle_seen_by_another_host(tmp_path, state.session_key)
        assert idle < timedelta(seconds=30)

    async def test_an_old_stamp_never_moves_last_tool_at_backwards(
        self, tmp_path: Path
    ) -> None:
        state = await _owner_with_stale_row(tmp_path)
        await update_current_session(state)  # a real biff call just now
        _stamp(tmp_path, datetime.now(UTC) - timedelta(hours=1))
        await _run_heartbeats(state, 0.1)
        idle = await _idle_seen_by_another_host(tmp_path, state.session_key)
        assert idle < timedelta(seconds=30)
