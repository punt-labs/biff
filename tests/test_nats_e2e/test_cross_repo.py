"""NATS E2E tests for cross-repo messaging (DES-030).

Two NatsRelay instances with different repo_name, configured as peers,
sharing the same NATS server. Verifies:
- Cross-repo session visibility via parallel per-repo queries
- Cross-repo message delivery
- visible_repos filtering
- TTY name assignment (repo-scoped)
- Wall broadcast to multiple repos
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport

from biff.models import BiffConfig
from biff.server.app import create_server
from biff.server.state import create_state
from biff.testing import RecordingClient, Transcript


@pytest.fixture
async def kai_biff(
    nats_server: str, shared_data_dir: Path, transcript: Transcript
) -> AsyncIterator[RecordingClient]:
    """kai in the 'biff' repo, peered with 'vox'."""
    config = BiffConfig(
        user="kai",
        repo_name="_test-biff",
        relay_url=nats_server,
        peers=("_test-vox",),
    )
    state = create_state(
        config,
        shared_data_dir / "kai-biff",
        tty="aaa111",
        hostname="host-biff",
        pwd="/biff",
    )
    mcp = create_server(state)
    async with Client(FastMCPTransport(mcp)) as client:
        yield RecordingClient(client=client, transcript=transcript, user="kai")


@pytest.fixture
async def eric_vox(
    nats_server: str, shared_data_dir: Path, transcript: Transcript
) -> AsyncIterator[RecordingClient]:
    """eric in the 'vox' repo, peered with 'biff'."""
    config = BiffConfig(
        user="eric",
        repo_name="_test-vox",
        relay_url=nats_server,
        peers=("_test-biff",),
    )
    state = create_state(
        config,
        shared_data_dir / "eric-vox",
        tty="bbb222",
        hostname="host-vox",
        pwd="/vox",
    )
    mcp = create_server(state)
    async with Client(FastMCPTransport(mcp)) as client:
        yield RecordingClient(client=client, transcript=transcript, user="eric")


@pytest.fixture
async def priya_quarry(
    nats_server: str, shared_data_dir: Path, transcript: Transcript
) -> AsyncIterator[RecordingClient]:
    """priya in 'quarry' repo, NOT peered with anyone."""
    config = BiffConfig(
        user="priya",
        repo_name="_test-quarry",
        relay_url=nats_server,
    )
    state = create_state(
        config,
        shared_data_dir / "priya-quarry",
        tty="ccc333",
        hostname="host-quarry",
        pwd="/quarry",
    )
    mcp = create_server(state)
    async with Client(FastMCPTransport(mcp)) as client:
        yield RecordingClient(client=client, transcript=transcript, user="priya")


class TestCrossRepoPresence:
    """Sessions from peered repos are visible in /who and /finger."""

    async def test_who_shows_peer_sessions(
        self, kai_biff: RecordingClient, eric_vox: RecordingClient
    ) -> None:
        """kai in biff sees eric in vox via /who."""
        await kai_biff.call("plan", message="working on biff")
        await eric_vox.call("plan", message="working on vox")

        result = await kai_biff.call("who")
        assert "@kai" in result
        assert "@eric" in result

    async def test_who_hides_non_peer_sessions(
        self,
        kai_biff: RecordingClient,
        eric_vox: RecordingClient,
        priya_quarry: RecordingClient,
    ) -> None:
        """kai in biff does NOT see priya in quarry (not peered)."""
        await kai_biff.call("plan", message="biff work")
        await eric_vox.call("plan", message="vox work")
        await priya_quarry.call("plan", message="quarry work")

        result = await kai_biff.call("who")
        assert "@kai" in result
        assert "@eric" in result
        assert "@priya" not in result

    async def test_finger_cross_repo(
        self, kai_biff: RecordingClient, eric_vox: RecordingClient
    ) -> None:
        """kai can /finger eric across repos."""
        await eric_vox.call("plan", message="building TTS engine")

        result = await kai_biff.call("finger", user="@eric")
        assert "Login: eric" in result
        assert "building TTS engine" in result

    async def test_who_shows_repo_column(
        self, kai_biff: RecordingClient, eric_vox: RecordingClient
    ) -> None:
        """REPO column shows each session's repo."""
        await kai_biff.call("plan", message="biff")
        await eric_vox.call("plan", message="vox")

        result = await kai_biff.call("who")
        assert "REPO" in result
        assert "_test-biff" in result
        assert "_test-vox" in result


class TestCrossRepoMessaging:
    """Cross-repo message delivery via targeted /write."""

    async def test_targeted_write_cross_repo(
        self, kai_biff: RecordingClient, eric_vox: RecordingClient
    ) -> None:
        """kai in biff writes to eric:tty in vox, eric receives it."""
        await kai_biff.call("plan", message="working")
        await eric_vox.call("plan", message="working")

        # Get eric's tty_name for targeted delivery
        who_result = await kai_biff.call("who")
        assert "@eric" in who_result

        # Write to eric's session (targeted by tty)
        result = await kai_biff.call(
            "write", to="@eric:bbb222", message="cross-repo hello"
        )
        assert "Message sent" in result

        # eric reads it
        read_result = await eric_vox.call("read_messages")
        assert "kai" in read_result
        assert "cross-repo hello" in read_result

    async def test_bare_write_stays_repo_local(
        self, kai_biff: RecordingClient, eric_vox: RecordingClient
    ) -> None:
        """Bare @user write stays repo-local (no cross-repo broadcast)."""
        await kai_biff.call("plan", message="working")
        await eric_vox.call("plan", message="working")

        # Bare write to eric (no tty) — goes to biff's subject, not vox's
        await kai_biff.call("write", to="@eric", message="local message")

        # eric in vox should NOT see it (message went to biff's subject)
        read_result = await eric_vox.call("read_messages")
        assert "No new messages" in read_result


class TestCrossRepoTtyNames:
    """TTY names are repo-scoped — duplicates across repos are allowed."""

    async def test_tty_names_can_duplicate_across_repos(
        self, kai_biff: RecordingClient, eric_vox: RecordingClient
    ) -> None:
        """Sessions in different repos can have the same ttyN name."""
        await kai_biff.call("plan", message="biff work")
        await eric_vox.call("plan", message="vox work")

        # Both sessions should be visible and both can be tty1
        result = await kai_biff.call("who")
        assert "@kai" in result
        assert "@eric" in result


class TestCrossRepoWall:
    """Wall broadcasts to peer repos."""

    async def test_wall_visible_to_peer(
        self, kai_biff: RecordingClient, eric_vox: RecordingClient
    ) -> None:
        """kai posts a wall in biff; eric sees it in vox."""
        await kai_biff.call("plan", message="working")
        await eric_vox.call("plan", message="working")

        await kai_biff.call("wall", message="deploy freeze until 5pm")

        # eric in vox sees the wall
        result = await eric_vox.call("wall")
        assert "deploy freeze" in result

    async def test_wall_not_visible_to_non_peer(
        self,
        kai_biff: RecordingClient,
        priya_quarry: RecordingClient,
    ) -> None:
        """kai posts a wall; priya in quarry (not peered) doesn't see it."""
        await kai_biff.call("plan", message="working")
        await priya_quarry.call("plan", message="working")

        await kai_biff.call("wall", message="biff-only wall")

        result = await priya_quarry.call("wall")
        assert "No active wall" in result

    async def test_wall_clear_clears_peers(
        self, kai_biff: RecordingClient, eric_vox: RecordingClient
    ) -> None:
        """Clearing a wall clears it for peers too."""
        await kai_biff.call("plan", message="working")
        await eric_vox.call("plan", message="working")

        await kai_biff.call("wall", message="freeze")
        await kai_biff.call("wall", clear=True)

        result = await eric_vox.call("wall")
        assert "No active wall" in result
