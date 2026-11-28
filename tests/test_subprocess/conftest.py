"""Fixtures for multi-process subprocess tests.

Each fixture spawns a real ``biff`` subprocess connected via stdio,
exercising the full wire protocol, CLI argument parsing, and process lifecycle.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Generator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastmcp.client.transports import StdioTransport
from mcp import Client as MCPClient, StdioServerParameters

from biff.caller_identity import CALLER_KEY
from biff.run_dir import RunDir
from biff.testing import NotificationTracker, RecordingClient, Transcript
from biff.testing.caller import StampedClient, caller_for

if TYPE_CHECKING:
    from fastmcp import Client

_TRANSCRIPT_DIR = Path(__file__).parent.parent / "transcripts"


def _make_transport(user: str, home: Path, repo: Path) -> StdioTransport:
    """Build a StdioTransport that spawns ``biff mcp`` for *user*, through biffd.

    The process runs in *repo* with *home* as its home directory, so it finds the
    biffd the ``run_dir`` fixture started there, as a person's first run does.
    """
    return StdioTransport(
        command=sys.executable,
        args=["-m", "biff", "mcp", "--user", user],
        env={
            "HOME": str(home),
            "PUNT_TELEMETRY": "off",
            "BIFF_MCP_TRANSPORT": "daemon",
        },
        cwd=str(repo),
    )


@pytest.fixture
def shared_data_dir(tmp_path: Path, run_dir: RunDir) -> Path:
    """The home directory the spawned servers and the real biffd share."""
    assert run_dir.port_path.exists()  # biffd is up before any server starts
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
async def biff_client(shared_data_dir: Path, repo: Path) -> AsyncIterator[Client[Any]]:
    """MCP client connected to a biff subprocess for user 'kai'."""
    user = "kai"
    transport = _make_transport(user, shared_data_dir, repo)
    async with StampedClient(transport, caller=caller_for(f"{user}-session")) as client:
        yield client


# --- Multi-user subprocess fixtures ---


@pytest.fixture
async def kai_client(shared_data_dir: Path, repo: Path) -> AsyncIterator[Client[Any]]:
    """MCP client connected to a biff subprocess for user 'kai'."""
    user = "kai"
    transport = _make_transport(user, shared_data_dir, repo)
    async with StampedClient(transport, caller=caller_for(f"{user}-session")) as client:
        yield client


@pytest.fixture
def modern_kai_client(shared_data_dir: Path, repo: Path) -> MCPClient:
    """Build a modern client to enter and exit in the same test task."""
    transport = _make_transport("kai", shared_data_dir, repo)
    return MCPClient(
        StdioServerParameters(
            command=transport.command,
            args=transport.args,
            env=transport.env,
            cwd=transport.cwd,
        )
    )


@pytest.fixture
async def legacy_kai_client(
    shared_data_dir: Path, repo: Path
) -> AsyncIterator[tuple[Client[Any], NotificationTracker]]:
    """Connect a handshake client whose server first attaches on a tool call."""
    transport = _make_transport("kai", shared_data_dir, repo)
    transport.env = {**(transport.env or {}), "CLAUDE_CODE_SESSION_ID": ""}
    tracker = NotificationTracker()
    async with StampedClient(
        transport, message_handler=tracker, mode="legacy"
    ) as client:
        yield client, tracker


@pytest.fixture
async def eric_client(shared_data_dir: Path, repo: Path) -> AsyncIterator[Client[Any]]:
    """MCP client connected to a biff subprocess for user 'eric'."""
    transport = _make_transport("eric", shared_data_dir, repo)
    async with StampedClient(transport, caller=caller_for("eric-session")) as client:
        yield client


@dataclass
class _OwnSession(RecordingClient):
    """A recording client whose every call declares its own user's session.

    biffd keys a session by its id, so two users cannot share the default one.
    """

    async def call(self, tool_name: str, **kwargs: object) -> str:
        kwargs.setdefault(CALLER_KEY, caller_for(f"{self.user}-session"))
        return await super().call(tool_name, **kwargs)


@pytest.fixture
async def kai(
    kai_client: Client[Any],
    transcript: Transcript,
) -> RecordingClient:
    """Recording client for kai over subprocess."""
    return _OwnSession(client=kai_client, transcript=transcript, user="kai")


@pytest.fixture
async def eric(
    eric_client: Client[Any],
    transcript: Transcript,
) -> RecordingClient:
    """Recording client for eric over subprocess."""
    return _OwnSession(client=eric_client, transcript=transcript, user="eric")


# --- Real biffd process ---

_DAEMON_STARTUP_TIMEOUT = 15.0


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repository, with biff enabled, the daemon can resolve."""
    path = tmp_path / "work" / "demo"
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)  # noqa: S603, S607
    marker = path / ".punt-labs" / "biff" / "enabled"
    marker.parent.mkdir(parents=True)
    marker.write_text("")  # biff is enabled here, as `biff enable` leaves it
    return path


@pytest.fixture
def run_dir(tmp_path: Path) -> Iterator[RunDir]:
    """A real ``biff daemon serve`` process in a throwaway home directory."""
    env = {**os.environ, "HOME": str(tmp_path), "PUNT_TELEMETRY": "off"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "biff", "daemon", "serve", "--port", "0"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    run = RunDir(tmp_path / ".punt-labs" / "biff" / "run")
    deadline = time.monotonic() + _DAEMON_STARTUP_TIMEOUT
    while not run.port_path.exists():
        assert time.monotonic() < deadline, "daemon never wrote its port file"
        time.sleep(0.05)
    yield run
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)
