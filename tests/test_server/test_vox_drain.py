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
