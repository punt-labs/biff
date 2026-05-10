"""Integration tests for dual-session registration (DES-039).

Verifies that when a CompanionSession is configured, both the primary
and companion sessions are visible in ``/who``, addressable via
``/write``, and that ``/read`` merges both inboxes.

Also verifies the single-session fallback when no companion is present.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport

from biff.models import BiffConfig
from biff.server.app import create_server
from biff.server.state import CompanionSession, ServerState, create_state
from biff.testing import RecordingClient, Transcript
from biff.tty import generate_tty

_TEST_REPO = "_test-dual-session"


@pytest.fixture
def shared_data_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def dual_state(shared_data_dir: Path) -> ServerState:
    """State with a companion session (dual-session mode)."""
    config = BiffConfig(
        user="claude",
        display_name="Claude Agento",
        kind="agent",
        repo_name=_TEST_REPO,
    )
    companion = CompanionSession(
        user="jfreeman",
        display_name="Jim Freeman",
        kind="human",
        tty=generate_tty(),
        tty_name="tty3",
    )
    return create_state(
        config,
        shared_data_dir,
        tty="aaa11111",
        hostname="test-host",
        pwd="/test",
        companion=companion,
        unread_path=shared_data_dir / "unread.json",
    )


@pytest.fixture
def single_state(shared_data_dir: Path) -> ServerState:
    """State without a companion session (single-session fallback)."""
    return create_state(
        BiffConfig(
            user="claude",
            display_name="Claude Agento",
            kind="agent",
            repo_name=_TEST_REPO,
        ),
        shared_data_dir,
        tty="bbb22222",
        hostname="test-host",
        pwd="/test",
        unread_path=shared_data_dir / "unread-single.json",
    )


@pytest.fixture
async def dual_client(dual_state: ServerState) -> AsyncIterator[Client[Any]]:
    from biff.server.app import _register_companion

    mcp = create_server(dual_state)
    async with Client(FastMCPTransport(mcp)) as client:
        # Companion registration is deferred to the heartbeat loop
        # (biff-8fg3). Tests that need a registered companion invoke
        # the helper directly after the lifespan opens, simulating
        # the first heartbeat tick.
        await _register_companion(dual_state)
        yield client


@pytest.fixture
async def single_client(
    single_state: ServerState,
) -> AsyncIterator[Client[Any]]:
    mcp = create_server(single_state)
    async with Client(FastMCPTransport(mcp)) as client:
        yield client


@pytest.fixture
def transcript() -> Transcript:
    return Transcript(title="")


@pytest.fixture
async def dual(dual_client: Client[Any], transcript: Transcript) -> RecordingClient:
    return RecordingClient(client=dual_client, transcript=transcript, user="claude")


@pytest.fixture
async def single(single_client: Client[Any], transcript: Transcript) -> RecordingClient:
    return RecordingClient(client=single_client, transcript=transcript, user="claude")


class TestDualSessionPresence:
    """Both primary and companion appear in /who."""

    async def test_who_shows_both_sessions(
        self, dual: RecordingClient, dual_state: ServerState
    ) -> None:
        result = await dual.call("who")
        assert "claude" in result
        assert "jfreeman" in result

    async def test_finger_primary(self, dual: RecordingClient) -> None:
        result = await dual.call("finger", user="claude")
        assert "claude" in result

    async def test_finger_companion(self, dual: RecordingClient) -> None:
        result = await dual.call("finger", user="jfreeman")
        assert "jfreeman" in result


class TestSingleSessionFallback:
    """Without companion, only the primary session is visible."""

    async def test_who_shows_only_primary(self, single: RecordingClient) -> None:
        result = await single.call("who")
        assert "claude" in result
        assert "jfreeman" not in result

    async def test_companion_is_none(self, single_state: ServerState) -> None:
        assert single_state.companion is None
        assert single_state.companion_session_key is None


class TestDualSessionMessaging:
    """Messages to companion inbox are visible via /read."""

    async def test_read_includes_companion_inbox(
        self,
        dual: RecordingClient,
        dual_state: ServerState,
    ) -> None:
        """Send a message to the companion user, then read from dual session."""
        # Deliver a message to the companion's user inbox.
        from biff.models import Message

        companion = dual_state.companion
        assert companion is not None
        msg = Message(
            from_user="eric",
            to_user=companion.user,
            body="lunch?",
        )
        await dual_state.relay.deliver(msg, sender_key="eric:fake1234")

        result = await dual.call("read_messages")
        assert "lunch?" in result

    async def test_read_dual_sections_per_identity(
        self,
        dual: RecordingClient,
        dual_state: ServerState,
    ) -> None:
        """Dual-session /read groups messages by identity."""
        from biff.models import Message

        companion = dual_state.companion
        assert companion is not None

        # Message to companion (human) inbox.
        await dual_state.relay.deliver(
            Message(from_user="kai", to_user=companion.user, body="hey Jim"),
            sender_key="kai:fake0001",
        )
        # Message to primary (agent) inbox.
        await dual_state.relay.deliver(
            Message(from_user="rmh", to_user=dual_state.config.user, body="impl done"),
            sender_key="rmh:fake0002",
        )

        result = await dual.call("read_messages")
        # Both section headers present.
        assert "\u25b6  jfreeman" in result
        assert "\u25b6  claude" in result
        # Human section appears first.
        assert result.index("\u25b6  jfreeman") < result.index("\u25b6  claude")

    async def test_read_single_session_no_sections(
        self,
        single: RecordingClient,
        single_state: ServerState,
    ) -> None:
        """Single-session /read has no section headers."""
        from biff.models import Message

        await single_state.relay.deliver(
            Message(
                from_user="kai", to_user=single_state.config.user, body="hey claude"
            ),
            sender_key="kai:fake0003",
        )

        result = await single.call("read_messages")
        assert "hey claude" in result
        assert "\u25b6  claude" not in result


class TestDualSessionStatusBar:
    """Status bar shows human identity in dual-session mode."""

    async def test_unread_file_shows_human_identity(
        self,
        dual: RecordingClient,
        dual_state: ServerState,
    ) -> None:
        """The unread JSON file uses companion user/tty_name."""
        # Trigger a tool call that writes the unread file.
        await dual.call("read_messages")
        assert dual_state.unread_path is not None
        data = json.loads(dual_state.unread_path.read_text())
        assert data["user"] == "jfreeman"
        companion = dual_state.companion
        assert companion is not None
        assert data["tty_name"] == companion.tty_name

    async def test_single_session_unread_file_shows_primary(
        self,
        single: RecordingClient,
        single_state: ServerState,
    ) -> None:
        """Without companion, unread file uses primary identity."""
        assert single_state.unread_path is not None
        await single.call("plan", message="solo work")
        data = json.loads(single_state.unread_path.read_text())
        assert data["user"] == "claude"
        assert data["tty_name"] != ""
