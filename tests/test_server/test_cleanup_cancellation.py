"""Cleanup survives cancellation and independent step failures."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from biff.models import BiffConfig
from biff.server import app
from biff.server.state import create_state
from biff.session_registry import register_session


async def test_cleanup_preserves_cancellation_when_teardown_fails() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def failed() -> None:
        entered.set()
        await release.wait()
        raise RuntimeError("cleanup failed")

    task = asyncio.create_task(app._finish_cleanup(failed()))
    await entered.wait()
    task.cancel("original")
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError, match="original"):
        await task


async def test_cleanup_propagates_uncancelled_failure() -> None:
    cleanup = AsyncMock(side_effect=RuntimeError("original failure"))
    with pytest.raises(RuntimeError, match="original failure"):
        await app._finish_cleanup(cleanup())


async def test_alias_inner_cancel_still_deletes_presence(tmp_path: Path) -> None:
    state = create_state(BiffConfig(user="rmh", repo_name="test"), tmp_path)
    await register_session(
        state.relay,
        "rmh",
        state.tty,
        display_name="rmh",
        kind="agent",
        hostname="h",
        pwd="/",
        repo="test",
    )
    with (
        patch.object(
            type(state.relay),
            "release_tty_name",
            side_effect=asyncio.CancelledError("inner alias"),
        ),
        pytest.raises(asyncio.CancelledError, match="inner alias"),
    ):
        await app._release_session(
            state, user="rmh", session_key=state.session_key, tty_name="tty1"
        )
    assert await state.relay.get_session(state.session_key) is None


@pytest.mark.parametrize("blocked_suffix", [".json", ".nudged"])
def test_unread_unlinks_independently(tmp_path: Path, blocked_suffix: str) -> None:
    unread = tmp_path / "status.json"
    sidecar = unread.with_suffix(".nudged")
    unread.write_text("status")
    sidecar.write_text("nudge")
    unlink = Path.unlink

    def denied(path: Path, missing_ok: bool = False) -> None:
        if path.suffix == blocked_suffix:
            raise PermissionError("denied")
        unlink(path, missing_ok=missing_ok)

    with patch.object(Path, "unlink", denied), pytest.raises(PermissionError):
        app._remove_unread_files(unread)
    assert unread.exists() == (blocked_suffix == ".json")
    assert sidecar.exists() == (blocked_suffix == ".nudged")
