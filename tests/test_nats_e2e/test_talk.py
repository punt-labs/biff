"""NATS E2E tests for talk — real-time conversation over NATS.

Two MCP servers (kai and eric) backed by NatsRelay.  Tests exercise
the full talk flow: initiation, blocking listen, message delivery
with instant notification, and session cleanup.
"""

from __future__ import annotations

import asyncio

import pytest

from biff.server.tools.talk import _reset_talk
from biff.testing import RecordingClient

pytestmark = pytest.mark.nats


@pytest.fixture(autouse=True)
def _clean_talk_state() -> None:  # pyright: ignore[reportUnusedFunction]
    """Reset module-level talk state between tests."""
    _reset_talk()


class TestTalkInitiation:
    """Starting a talk session."""

    async def test_talk_starts_session(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """kai can start a talk session with eric."""
        # eric must be online (set a plan to register session)
        await eric.call("plan", message="available")

        result = await kai.call("talk", to="@eric", message="hey, review my PR?")
        assert "Talk session started" in result
        assert "@eric" in result

    async def test_talk_with_opening_message_delivers(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """Opening message in /talk is delivered to recipient's inbox."""
        await eric.call("plan", message="available")

        await kai.call("talk", to="@eric", message="check PR #42")
        result = await eric.call("read_messages")
        assert "check PR #42" in result

    async def test_talk_offline_user(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """Talk to an offline user returns error."""
        # eric has NOT registered a session
        result = await kai.call("talk", to="@nobody")
        assert "not online" in result


class TestTalkListen:
    """Blocking receive via talk_listen."""

    async def test_listen_returns_existing_messages(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """talk_listen returns messages already in the inbox."""
        await eric.call("plan", message="available")
        await kai.call("write", to="@eric", message="are you there?")

        # eric calls talk_listen — message is already waiting
        result = await eric.call("talk_listen", timeout=2)
        assert "are you there?" in result
        assert "@kai" in result

    async def test_listen_timeout_no_messages(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """talk_listen returns timeout message when no messages arrive."""
        await eric.call("plan", message="available")

        result = await eric.call("talk_listen", timeout=1)
        assert "No new messages" in result

    async def test_listen_wakes_on_notification(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """talk_listen wakes instantly when a message is delivered."""
        await eric.call("plan", message="available")
        await kai.call("plan", message="available")

        # Start eric listening in background with long timeout
        listen_task = asyncio.create_task(eric.call("talk_listen", timeout=10))

        # Give subscription time to be established
        await asyncio.sleep(0.3)

        # kai sends a message — notification wakes eric
        await kai.call("write", to="@eric", message="urgent: deploy broken")

        # eric should receive quickly (not wait the full 10s)
        result = await asyncio.wait_for(listen_task, timeout=5.0)
        assert "urgent: deploy broken" in result
        assert "@kai" in result

    async def test_listen_format_is_chat_style(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """talk_listen formats messages in chat style, not table."""
        await eric.call("plan", message="available")
        await kai.call("write", to="@eric", message="hello from talk")

        result = await eric.call("talk_listen", timeout=2)
        # Chat format: [HH:MM:SS] @user: message
        assert "] @kai: hello from talk" in result


class TestTalkEnd:
    """Ending a talk session."""

    async def test_end_active_session(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """talk_end closes an active session."""
        await eric.call("plan", message="available")
        await kai.call("talk", to="@eric")

        result = await kai.call("talk_end")
        assert "Talk session with @eric ended" in result

    async def test_end_no_session(self, kai: RecordingClient) -> None:
        """talk_end with no active session returns error."""
        result = await kai.call("talk_end")
        assert "No active talk session" in result


class TestTalkConversation:
    """Full talk conversation flow."""

    @pytest.mark.transcript
    async def test_full_talk_flow(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """Complete talk conversation: initiate, exchange, end."""
        kai.transcript.title = "NATS E2E: talk conversation"
        kai.transcript.description = (
            "Real-time conversation between two MCP servers via NATS."
        )

        # Both online
        await kai.call("plan", message="implementing talk")
        await eric.call("plan", message="reviewing PRs")

        # kai initiates talk with opening message
        result = await kai.call("talk", to="@eric", message="can you review PR #42?")
        assert "Talk session started" in result

        # eric receives the opening message via talk_listen
        result = await eric.call("talk_listen", timeout=2)
        assert "can you review PR #42?" in result

        # eric replies via write
        await eric.call("write", to="@kai", message="sure, looking now")

        # kai receives reply via talk_listen
        result = await kai.call("talk_listen", timeout=2)
        assert "sure, looking now" in result

        # kai ends the talk session
        result = await kai.call("talk_end")
        assert "ended" in result
