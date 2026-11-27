"""The daemon's heartbeat carries the hook's activity stamp into ``last_tool_at``.

A session that works through Bash and Edit never calls a biff tool, so only the
PostToolUse hook's ``<session_id>.active`` stamp says it is busy.  A plain
heartbeat would show it hours idle on every other host.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from biff.relay import LocalRelay

if TYPE_CHECKING:
    from pathlib import Path

    from tests.test_daemon.conftest import Harness

_TEN_MINUTES = timedelta(minutes=10)
_ONE_MINUTE = timedelta(minutes=1)
_SETTLE_SECONDS = 0.4  # several heartbeats at the harness's 0.05 s interval


def _stamp(harness: Harness, session_id: str, when: datetime) -> Path:
    path = harness.unread_dir / f"{session_id}.active"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    os.utime(path, (when.timestamp(), when.timestamp()))
    return path


async def _stale_session(harness: Harness, session_id: str) -> tuple[LocalRelay, str]:
    """Attach a Claude session whose row says its last tool call was 10m ago."""
    mcp = await harness.connect(session_id, "kai", client="mcp")
    status = cast("dict[str, dict[str, str]]", await mcp.request("status"))
    key = status["data"]["session_key"]
    viewer = LocalRelay(harness.factory.built[0])  # another process, same relay
    row = await viewer.get_session(key)
    assert row is not None
    stale = datetime.now(UTC) - _TEN_MINUTES
    await viewer.update_session(row.model_copy(update={"last_tool_at": stale}))
    return viewer, key


async def _idle(viewer: LocalRelay, key: str) -> timedelta:
    row = await viewer.get_session(key)
    assert row is not None
    return datetime.now(UTC) - row.last_tool_at


async def test_a_stamp_from_bash_and_edit_keeps_the_session_active(
    harness: Harness,
) -> None:
    viewer, key = await _stale_session(harness, "s-busy")
    _stamp(harness, "s-busy", datetime.now(UTC) - _ONE_MINUTE)
    await asyncio.sleep(_SETTLE_SECONDS)
    assert await _idle(viewer, key) < _TEN_MINUTES / 2


async def test_without_a_stamp_the_session_stays_idle(harness: Harness) -> None:
    viewer, key = await _stale_session(harness, "s-quiet")
    await asyncio.sleep(_SETTLE_SECONDS)
    assert await _idle(viewer, key) >= _TEN_MINUTES


async def test_a_stale_stamp_never_moves_the_clock_backwards(
    harness: Harness,
) -> None:
    viewer, key = await _stale_session(harness, "s-old")
    _stamp(harness, "s-old", datetime.now(UTC) - 2 * _TEN_MINUTES)
    await asyncio.sleep(_SETTLE_SECONDS)
    assert await _idle(viewer, key) < _TEN_MINUTES + _ONE_MINUTE


async def test_another_sessions_stamp_does_not_count(harness: Harness) -> None:
    viewer, key = await _stale_session(harness, "s-mine")
    _stamp(harness, "s-theirs", datetime.now(UTC))
    await asyncio.sleep(_SETTLE_SECONDS)
    assert await _idle(viewer, key) >= _TEN_MINUTES
