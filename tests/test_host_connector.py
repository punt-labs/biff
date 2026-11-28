"""HostConnector keeps a slow service manager inside the caller's deadline."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import pytest

from biff.client.connection import ClientIdentity
from biff.client.host import HostConnector
from biff.client.nudge import DaemonNudge
from biff.run_dir import RunDir

if TYPE_CHECKING:
    from pathlib import Path

_SLOW = 3.0


def _slow_for_host(_run_dir: RunDir) -> DaemonNudge:
    time.sleep(_SLOW)  # a service manager that does not answer
    raise AssertionError


def test_a_slow_service_manager_does_not_outlive_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(DaemonNudge, "for_host", staticmethod(_slow_for_host))
    connector = HostConnector(RunDir(tmp_path / "run"))
    identity = ClientIdentity(client="hook", session_id="s1", cwd=str(tmp_path))

    async def attempt() -> None:
        async with asyncio.timeout(0.2):
            await connector.connect(identity)

    start = time.monotonic()
    with pytest.raises(TimeoutError):
        asyncio.run(attempt())

    assert time.monotonic() - start < _SLOW / 2
