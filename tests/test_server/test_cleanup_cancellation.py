"""Lifespan cancellation cannot release a pin ahead of teardown tasks."""

from __future__ import annotations

import asyncio
from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from biff.ethos_session import EthosSessionPin
from biff.models import BiffConfig
from biff.server import app
from biff.server.state import create_state


@pytest.mark.parametrize(
    ("step", "owns_relay"),
    [("logout", True), ("shutdown", True), ("shutdown", False), ("close", True)],
)
@pytest.mark.parametrize("cancel_body", [False, True])
async def test_cancel_after_yield_joins_teardown_before_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    step: str,
    owns_relay: bool,
    cancel_body: bool,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    pin = EthosSessionPin("ses_cancel123", "ses_cancel123")
    state = create_state(
        BiffConfig(user="rmh", repo_name="test"),
        tmp_path / "relay",
        tty=pin.routing_id,
        unread_path=pin.unread_path(tmp_path / "unread"),
        ethos_session_pin=pin,
        auto_companion=False,
    )
    object.__setattr__(state, "owns_relay", owns_relay)
    entered = asyncio.Event()
    release = asyncio.Event()
    ready = asyncio.Event()
    leave = asyncio.Event()
    workers: list[asyncio.Task[None]] = []
    original_logout = app._append_logout_event
    original_shutdown = app._shutdown_tasks
    original_close = state.relay.close

    async def pause() -> None:
        entered.set()
        await release.wait()

    async def logout(*args: object) -> None:
        if step == "logout":
            await pause()
        await original_logout(state)

    async def shutdown(event: asyncio.Event, tasks: list[asyncio.Task[None]]) -> None:
        workers.extend(tasks)
        if step == "shutdown":
            await pause()
        await original_shutdown(event, tasks)

    async def close() -> None:
        if step == "close":
            await pause()
        await original_close()

    async def owner() -> None:
        with pin.ownership():
            async with app._active_lifespan(app.create_server(state), state):
                ready.set()
                await leave.wait()

    with (
        patch("biff.server.app.signal.signal"),
        patch.object(app, "_append_logout_event", logout),
        patch.object(app, "_shutdown_tasks", shutdown),
        patch.object(type(state.relay), "close", side_effect=close) as closed,
    ):
        task = asyncio.create_task(owner())
        await asyncio.wait_for(ready.wait(), 5)
        if cancel_body:
            task.cancel("first cancellation")
        else:
            leave.set()
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel("second cancellation" if cancel_body else "first cancellation")
        await asyncio.sleep(0)
        task.cancel("second cancellation")
        await asyncio.sleep(0)
        assert not task.done()
        with pytest.raises(ValueError, match="already has a running"), pin.ownership():
            pytest.fail("ownership escaped while teardown was blocked")
        release.set()
        with pytest.raises(asyncio.CancelledError, match="first cancellation"):
            await task
        assert workers and all(worker.done() for worker in workers)
        assert closed.await_count == int(owns_relay)
    assert state.unread_path is not None and not state.unread_path.exists()
    # The same pin can now register; all predecessor workers are joined and
    # cannot heartbeat, poll, or delete the replacement after ownership moves.
    with pin.ownership():
        replacement, _ = await app.register_session(
            state.relay,
            "rmh",
            pin.routing_id,
            display_name="replacement",
            kind="agent",
            hostname="h",
            pwd="/",
            repo="test",
        )
        await asyncio.sleep(0.01)
        assert await state.relay.get_session(state.session_key) == replacement


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


@pytest.mark.parametrize("failure_step", ["workers", "logout", "vox", "unread"])
async def test_stage_failure_finishes_remaining_cleanup_under_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_step: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    pin = EthosSessionPin("ses_timeout123", "ses_timeout123")
    state = create_state(
        BiffConfig(user="rmh", repo_name="test"),
        tmp_path / "relay",
        tty=pin.routing_id,
        unread_path=pin.unread_path(tmp_path / "unread"),
        ethos_session_pin=pin,
        auto_companion=False,
    )
    cancelled = asyncio.Event()
    worker: asyncio.Task[None] | None = None
    shutdown = app._shutdown_tasks
    remove = app._remove_unread_files

    async def slow_worker() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def workers(event: asyncio.Event, tasks: list[asyncio.Task[None]]) -> None:
        nonlocal worker
        worker = asyncio.create_task(slow_worker())
        await asyncio.sleep(0)
        await shutdown(event, [*tasks, worker], timeout=0.01)

    async def failure(*args: object) -> None:
        raise RuntimeError("stage failure")

    def unread(path: Path | None) -> None:
        remove(path)
        raise RuntimeError("stage failure")

    async def close() -> None:
        with pytest.raises(ValueError, match="already has a running"), pin.ownership():
            pytest.fail("lock escaped before close")

    target: AbstractContextManager[object]
    if failure_step == "workers":
        target = patch.object(app, "_shutdown_tasks", workers)
    elif failure_step == "logout":
        target = patch.object(app, "_append_logout_event", failure)
    elif failure_step == "vox":
        target = patch("biff.integration.vox.drain_background_tasks", failure)
    else:
        target = patch.object(app, "_remove_unread_files", unread)
    with (
        patch("biff.server.app.signal.signal"),
        target,
        patch.object(type(state.relay), "close", side_effect=close) as closed,
        pin.ownership(),
        pytest.raises(TimeoutError if failure_step == "workers" else RuntimeError),
    ):
        async with app._active_lifespan(app.create_server(state), state):
            pass
    closed.assert_awaited_once()
    if worker is not None:
        assert worker.done() and cancelled.is_set()
    assert not await state.relay.get_sessions()
    assert state.unread_path is not None and not state.unread_path.exists()
    assert not list((tmp_path / ".punt-labs/biff/active").iterdir())
    with pin.ownership():
        pass


async def test_startup_cleanup_failure_still_closes_and_preserves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    pin = EthosSessionPin("ses_startup123", "ses_startup123")
    state = create_state(
        BiffConfig(user="rmh", repo_name="test"),
        tmp_path / "relay",
        tty=pin.routing_id,
        ethos_session_pin=pin,
        auto_companion=False,
    )
    with (
        patch("biff.server.app.signal.signal"),
        patch.object(app, "refresh_wall", side_effect=RuntimeError("original")),
        patch.object(app, "_remove_unread_files", side_effect=OSError("cleanup")),
        patch.object(type(state.relay), "close", new_callable=AsyncMock) as closed,
        pin.ownership(),
        pytest.raises(RuntimeError, match="original"),
    ):
        async with app._active_lifespan(app.create_server(state), state):
            pytest.fail("startup yielded")
    closed.assert_awaited_once()
    assert not await state.relay.get_sessions()


async def test_alias_inner_cancel_still_deletes_presence(tmp_path: Path) -> None:
    state = create_state(BiffConfig(user="rmh", repo_name="test"), tmp_path)
    await app.register_session(
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
