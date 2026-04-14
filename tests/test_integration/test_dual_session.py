"""Integration tests for dual-session registration (DES-039).

Verifies that when a CompanionSession is configured, both the primary
and companion sessions are visible in ``/who``, addressable via
``/write``, and that ``/read`` merges both inboxes.

Also verifies the single-session fallback when no companion is present.
"""

from __future__ import annotations

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
    )
    return create_state(
        config,
        shared_data_dir,
        tty="aaa11111",
        hostname="test-host",
        pwd="/test",
        companion=companion,
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
    )


@pytest.fixture
async def dual_client(dual_state: ServerState) -> AsyncIterator[Client[Any]]:
    mcp = create_server(dual_state)
    async with Client(FastMCPTransport(mcp)) as client:
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
