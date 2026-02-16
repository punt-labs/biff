"""Fixtures for NATS E2E tests.

Two full MCP servers backed by NatsRelay, connected via in-memory
FastMCPTransport. Exercises the same tool interactions as subprocess
tests but with NATS as the relay backend instead of the filesystem.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Generator
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

import nats
import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport

from biff.models import BiffConfig
from biff.server.app import create_server
from biff.server.state import create_state
from biff.testing import RecordingClient, Transcript

_TRANSCRIPT_DIR = Path(__file__).parent.parent / "transcripts"
_TEST_REPO = "_test-nats-e2e"


@pytest.fixture(autouse=True)
async def _cleanup_nats(nats_server: str) -> AsyncIterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Delete NATS streams and KV buckets after each test for isolation."""
    yield
    nc = await nats.connect(nats_server)  # pyright: ignore[reportUnknownMemberType]
    js = nc.jetstream()  # pyright: ignore[reportUnknownMemberType]
    with suppress(Exception):
        await js.delete_stream(f"BIFF_{_TEST_REPO}_INBOX")
    with suppress(Exception):
        await js.delete_key_value(f"biff-{_TEST_REPO}-sessions")  # pyright: ignore[reportUnknownMemberType]
    await nc.close()


@pytest.fixture
def shared_data_dir(tmp_path: Path) -> Path:
    """Shared data directory for unread state files."""
    return tmp_path


@pytest.fixture
def transcript(request: pytest.FixtureRequest) -> Generator[Transcript]:
    t = Transcript(title="")
    yield t
    node = cast("pytest.Item", request.node)  # pyright: ignore[reportUnknownMemberType]
    marker = node.get_closest_marker("transcript")
    if marker and t.entries:
        _TRANSCRIPT_DIR.mkdir(exist_ok=True)
        slug = node.name.replace("[", "_").replace("]", "")
        path = _TRANSCRIPT_DIR / f"{slug}.txt"
        path.write_text(t.render())


@pytest.fixture
async def kai_client(
    nats_server: str, shared_data_dir: Path
) -> AsyncIterator[Client[Any]]:
    """MCP client for kai backed by NatsRelay."""
    config = BiffConfig(user="kai", repo_name=_TEST_REPO, relay_url=nats_server)
    state = create_state(config, shared_data_dir / "kai")
    mcp = create_server(state)
    async with Client(FastMCPTransport(mcp)) as client:
        yield client


@pytest.fixture
async def eric_client(
    nats_server: str, shared_data_dir: Path
) -> AsyncIterator[Client[Any]]:
    """MCP client for eric backed by NatsRelay."""
    config = BiffConfig(user="eric", repo_name=_TEST_REPO, relay_url=nats_server)
    state = create_state(config, shared_data_dir / "eric")
    mcp = create_server(state)
    async with Client(FastMCPTransport(mcp)) as client:
        yield client


@pytest.fixture
async def kai(
    kai_client: Client[Any],
    transcript: Transcript,
) -> RecordingClient:
    """Recording client for kai in NATS E2E tests."""
    return RecordingClient(client=kai_client, transcript=transcript, user="kai")


@pytest.fixture
async def eric(
    eric_client: Client[Any],
    transcript: Transcript,
) -> RecordingClient:
    """Recording client for eric in NATS E2E tests."""
    return RecordingClient(client=eric_client, transcript=transcript, user="eric")
