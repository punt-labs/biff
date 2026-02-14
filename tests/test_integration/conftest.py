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


@pytest.fixture
def config() -> BiffConfig:
    return BiffConfig(user="kai")


@pytest.fixture
def state(tmp_path: Path, config: BiffConfig) -> ServerState:
    return create_state(config, tmp_path)


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
