"""Real child processes exercise shutdown during pending Vox creation."""

from __future__ import annotations

import asyncio
import sys
from contextlib import suppress
from unittest.mock import patch

import pytest

from biff.integration import vox


@pytest.mark.vox
@pytest.mark.parametrize("pending_spawn", [False, True])
async def test_drain_joins_real_child_even_when_spawn_returns_after_cancel(
    pending_spawn: bool,
) -> None:
    entered = asyncio.Event()
    processes: list[asyncio.subprocess.Process] = []
    create = asyncio.create_subprocess_exec

    async def spawn(*args: str, **kwargs: object) -> asyncio.subprocess.Process:
        if pending_spawn:
            entered.set()
            with suppress(asyncio.CancelledError):
                await asyncio.Event().wait()
        proc = await create(
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready',flush=True); time.sleep(60)",
            stdout=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None
        await proc.stdout.readline()
        processes.append(proc)
        entered.set()
        return proc

    with (
        patch.object(vox, "vox_binary", return_value=sys.executable),
        patch.object(vox, "_probe_once_support", return_value=False),
        patch.object(vox, "_SHUTDOWN_TIMEOUT", 0.1),
        patch("asyncio.create_subprocess_exec", spawn),
    ):
        vox.speak_fire_and_forget("test")
        await entered.wait()
        await asyncio.wait_for(vox.drain_background_tasks(), timeout=2)
    assert processes and all(proc.returncode is not None for proc in processes)
    assert not vox._spawned_procs
    assert not vox._background_tasks


@pytest.mark.vox
async def test_late_creation_has_owner_without_second_drain() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    processes: list[asyncio.subprocess.Process] = []
    create = asyncio.create_subprocess_exec

    async def delayed(*args: str, **kwargs: object) -> asyncio.subprocess.Process:
        entered.set()
        while not release.is_set():
            with suppress(asyncio.CancelledError):
                await release.wait()
        proc = await create(sys.executable, "-c", "import time; time.sleep(60)")
        processes.append(proc)
        return proc

    with (
        patch.object(vox, "_SHUTDOWN_TIMEOUT", 0.02),
        patch("asyncio.create_subprocess_exec", delayed),
    ):
        task = asyncio.create_task(vox._spawn([sys.executable]))
        vox._background_tasks.add(task)
        task.add_done_callback(vox._background_tasks.discard)
        await entered.wait()
        with pytest.raises(TimeoutError):
            await vox.drain_background_tasks()
        assert vox._creating and vox._draining
        release.set()
        # Await creation callbacks and their real child-reaping tasks, without
        # calling drain again (the original unmanaged-late-result defect).
        await asyncio.gather(*tuple(vox._creating), return_exceptions=True)
        await asyncio.sleep(0)
        await asyncio.gather(*tuple(vox._background_tasks), return_exceptions=True)
        assert processes and all(proc.returncode is not None for proc in processes)
        assert not vox._creating and not vox._spawned_procs
        await vox.drain_background_tasks()
        assert not vox._draining


@pytest.mark.vox
async def test_failed_executable_does_not_block_neighbor_or_repeat_drain() -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)"
    )
    vox._spawned_procs.add(proc)
    await vox._spawn(["/nonexistent-biff-vox-executable"])
    await asyncio.sleep(0)
    assert not vox._creating
    await asyncio.gather(vox.drain_background_tasks(), vox.drain_background_tasks())
    assert proc.returncode is not None
    assert not vox._spawned_procs and not vox._creating and not vox._draining
