"""Fixtures for multi-process subprocess tests.

Each fixture spawns a real ``biff`` subprocess connected via stdio,
exercising the full wire protocol, CLI argument parsing, and process lifecycle.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Generator
from pathlib import Path
from typing import Any, cast

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from biff.testing import RecordingClient, Transcript

_TRANSCRIPT_DIR = Path(__file__).parent.parent / "transcripts"


def _make_transport(user: str, data_dir: Path) -> StdioTransport:
    """Build a StdioTransport that spawns ``biff`` for the given user.

    Passes ``--relay-url ""`` to force local relay, preventing the
    subprocess from connecting to a remote NATS server configured
    in the repo's ``.biff`` file.
    """
    return StdioTransport(
        command="uv",
        args=[
            "run",
            "biff",
            "serve",
            "--user",
            user,
            "--data-dir",
            str(data_dir),
            "--relay-url",
            "",
            "--transport",
            "stdio",
        ],
    )


@pytest.fixture
def shared_data_dir(tmp_path: Path) -> Path:
    """Shared data directory for cross-process state."""
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


# --- Single-user subprocess fixtures ---


@pytest.fixture
async def biff_client(shared_data_dir: Path) -> AsyncIterator[Client[Any]]:
    """MCP client connected to a biff subprocess for user 'kai'."""
    transport = _make_transport("kai", shared_data_dir)
    async with Client(transport) as client:
        yield client


# --- Multi-user subprocess fixtures ---


@pytest.fixture
async def kai_client(shared_data_dir: Path) -> AsyncIterator[Client[Any]]:
    """MCP client connected to a biff subprocess for user 'kai'."""
    transport = _make_transport("kai", shared_data_dir)
    async with Client(transport) as client:
        yield client


@pytest.fixture
async def eric_client(shared_data_dir: Path) -> AsyncIterator[Client[Any]]:
    """MCP client connected to a biff subprocess for user 'eric'."""
    transport = _make_transport("eric", shared_data_dir)
    async with Client(transport) as client:
        yield client


@pytest.fixture
async def kai(
    kai_client: Client[Any],
    transcript: Transcript,
) -> RecordingClient:
    """Recording client for kai over subprocess."""
    return RecordingClient(client=kai_client, transcript=transcript, user="kai")


@pytest.fixture
async def eric(
    eric_client: Client[Any],
    transcript: Transcript,
) -> RecordingClient:
    """Recording client for eric over subprocess."""
    return RecordingClient(client=eric_client, transcript=transcript, user="eric")
