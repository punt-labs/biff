"""Fixtures for MCP protocol integration tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Generator
from pathlib import Path
from typing import Any, cast

import pytest
from fastmcp import Client
from fastmcp.client.client import CallToolResult
from fastmcp.client.transports import FastMCPTransport

from biff.models import BiffConfig
from biff.server.app import create_server
from biff.server.state import ServerState, create_state
from biff.testing import RecordingClient, Transcript

__all__ = ["CallToolResult"]

_TRANSCRIPT_DIR = Path(__file__).parent.parent / "transcripts"
_TEST_REPO = "_test-integration"


@pytest.fixture
def config() -> BiffConfig:
    return BiffConfig(user="kai", repo_name=_TEST_REPO)


@pytest.fixture
def state(tmp_path: Path, config: BiffConfig) -> ServerState:
    return create_state(config, tmp_path, tty="tty1", hostname="test-host", pwd="/test")


@pytest.fixture
async def biff_client(state: ServerState) -> AsyncIterator[Client[Any]]:
    """MCP client connected to a biff server via in-memory transport."""
    mcp = create_server(state)
    async with Client(FastMCPTransport(mcp)) as client:
        yield client


@pytest.fixture
def transcript(request: pytest.FixtureRequest) -> Generator[Transcript]:
    t = Transcript(title="")
    yield t
    # After test completes, write transcript if marked and non-empty
    node = cast("pytest.Item", request.node)  # pyright: ignore[reportUnknownMemberType]
    marker = node.get_closest_marker("transcript")
    if marker and t.entries:
        _TRANSCRIPT_DIR.mkdir(exist_ok=True)
        slug = node.name.replace("[", "_").replace("]", "")
        path = _TRANSCRIPT_DIR / f"{slug}.txt"
        path.write_text(t.render())


@pytest.fixture
async def recorder(
    biff_client: Client[Any],
    transcript: Transcript,
) -> RecordingClient:
    """Recording client that captures tool calls into a transcript."""
    return RecordingClient(client=biff_client, transcript=transcript)


# --- E2E fixtures: two users sharing state via the same data directory ---


@pytest.fixture
def shared_data_dir(tmp_path: Path) -> Path:
    """Shared data directory for multi-user E2E tests."""
    return tmp_path


@pytest.fixture
def kai_state(shared_data_dir: Path) -> ServerState:
    """Server state for user kai."""
    return create_state(
        BiffConfig(user="kai", repo_name=_TEST_REPO),
        shared_data_dir,
        tty="tty1",
        hostname="test-host",
        pwd="/test",
    )


@pytest.fixture
def eric_state(shared_data_dir: Path) -> ServerState:
    """Server state for user eric."""
    return create_state(
        BiffConfig(user="eric", repo_name=_TEST_REPO),
        shared_data_dir,
        tty="tty2",
        hostname="test-host",
        pwd="/test",
    )


@pytest.fixture
async def kai_client(kai_state: ServerState) -> AsyncIterator[Client[Any]]:
    """MCP client for user kai."""
    mcp = create_server(kai_state)
    async with Client(FastMCPTransport(mcp)) as client:
        yield client


@pytest.fixture
async def eric_client(eric_state: ServerState) -> AsyncIterator[Client[Any]]:
    """MCP client for user eric."""
    mcp = create_server(eric_state)
    async with Client(FastMCPTransport(mcp)) as client:
        yield client


@pytest.fixture
async def kai(
    kai_client: Client[Any],
    transcript: Transcript,
) -> RecordingClient:
    """Recording client for kai in E2E tests."""
    return RecordingClient(client=kai_client, transcript=transcript, user="kai")


@pytest.fixture
async def eric(
    eric_client: Client[Any],
    transcript: Transcript,
) -> RecordingClient:
    """Recording client for eric in E2E tests."""
    return RecordingClient(client=eric_client, transcript=transcript, user="eric")
