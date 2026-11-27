"""``biff daemon serve``: listener, /health, token and port files."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from importlib.metadata import version as pkg_version
from typing import TYPE_CHECKING, cast

import pytest

from biff.daemon.server import BiffDaemon
from biff.run_dir import RunDir

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


async def http_get(port: int, path: str) -> tuple[int, str]:
    """Return ``(status, body)`` of an unauthenticated GET."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    request = f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
    writer.write(request.encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split()[1])
    return status, body.decode()


async def health(port: int) -> dict[str, object]:
    """Return the decoded ``/health`` document."""
    status, body = await http_get(port, "/health")
    assert status == 200
    return cast("dict[str, object]", json.loads(body))


@pytest.fixture
def run_dir(tmp_path: Path) -> RunDir:
    return RunDir(tmp_path / "run")


@pytest.fixture
async def daemon(run_dir: RunDir) -> AsyncIterator[BiffDaemon]:
    daemon = BiffDaemon(run_dir=run_dir, port=0)
    await daemon.start()
    yield daemon
    await daemon.stop()


async def test_health_answers_with_version_uptime_and_pid_only(
    daemon: BiffDaemon,
) -> None:
    assert set(await health(daemon.port)) == {"version", "uptime_seconds", "pid"}


async def test_health_reports_the_daemons_own_pid(daemon: BiffDaemon) -> None:
    assert (await health(daemon.port))["pid"] == os.getpid()


async def test_health_reports_the_package_version(daemon: BiffDaemon) -> None:
    assert (await health(daemon.port))["version"] == pkg_version("punt-biff")


async def test_health_declares_one_json_content_type(daemon: BiffDaemon) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", daemon.port)
    writer.write(b"GET /health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    await writer.drain()
    head = (await reader.read()).partition(b"\r\n\r\n")[0].decode().lower()
    writer.close()
    assert head.count("content-type:") == 1
    assert "application/json" in head


async def test_ephemeral_port_is_never_the_default(daemon: BiffDaemon) -> None:
    assert daemon.port not in (0, 8424)


async def test_unknown_path_is_404(daemon: BiffDaemon) -> None:
    status, _ = await http_get(daemon.port, "/state")
    assert status == 404


async def test_token_file_exists_at_0600(daemon: BiffDaemon, run_dir: RunDir) -> None:
    assert stat.S_IMODE(run_dir.token_path.stat().st_mode) == 0o600


async def test_port_file_names_the_bound_port(
    daemon: BiffDaemon, run_dir: RunDir
) -> None:
    assert run_dir.read_port() == daemon.port


async def test_clean_shutdown_removes_the_port_file(run_dir: RunDir) -> None:
    daemon = BiffDaemon(run_dir=run_dir, port=0)
    await daemon.start()
    await daemon.stop()
    assert not run_dir.port_path.exists()


async def test_token_survives_a_restart(run_dir: RunDir) -> None:
    first = BiffDaemon(run_dir=run_dir, port=0)
    await first.start()
    token = run_dir.read_token()
    await first.stop()
    second = BiffDaemon(run_dir=run_dir, port=0)
    await second.start()
    try:
        assert run_dir.read_token() == token
    finally:
        await second.stop()
