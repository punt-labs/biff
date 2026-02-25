"""NATS E2E tests for talk — real-time conversation over NATS.

Two MCP servers (kai and eric) backed by NatsRelay.  Tests exercise
the full talk flow: initiation, blocking listen, message delivery
with instant notification, and session cleanup.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import nats as nats_lib
import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport

from biff.models import BiffConfig
from biff.server.app import create_server
from biff.server.state import create_state
from biff.server.tools.talk import _reset_talk
from biff.testing import RecordingClient, Transcript

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


_TEST_REPO = "_test-nats-e2e"


class TestTalkTtyNameResolution:
    """Talk resolves friendly tty_name to actual session key for delivery."""

    async def test_talk_resolves_tty_name(
        self,
        nats_server: str,
        shared_data_dir: Path,
        transcript: Transcript,
    ) -> None:
        """Opening message via /talk @user:tty_name reaches the correct session."""
        # eric has two sessions: tty_a and tty_b (hex IDs).
        # tty_a names itself "dev-laptop" via /tty.
        # kai talks to @eric:dev-laptop — the message must reach tty_a, not tty_b.
        eric_cfg = BiffConfig(user="eric", repo_name=_TEST_REPO, relay_url=nats_server)
        eric_a_state = create_state(
            eric_cfg,
            shared_data_dir / "eric-a",
            tty="aaaa1111",
            hostname="test",
            pwd="/test",
        )
        eric_b_state = create_state(
            eric_cfg,
            shared_data_dir / "eric-b",
            tty="bbbb2222",
            hostname="test",
            pwd="/test",
        )
        kai_cfg = BiffConfig(user="kai", repo_name=_TEST_REPO, relay_url=nats_server)
        kai_state = create_state(
            kai_cfg,
            shared_data_dir / "kai",
            tty="cccc3333",
            hostname="test",
            pwd="/test",
        )

        eric_a_mcp = create_server(eric_a_state)
        eric_b_mcp = create_server(eric_b_state)
        kai_mcp = create_server(kai_state)

        async with (
            Client(FastMCPTransport(eric_a_mcp)) as eric_a_raw,
            Client(FastMCPTransport(eric_b_mcp)) as eric_b_raw,
            Client(FastMCPTransport(kai_mcp)) as kai_raw,
        ):
            eric_a = RecordingClient(
                client=eric_a_raw, transcript=transcript, user="eric-a"
            )
            eric_b = RecordingClient(
                client=eric_b_raw, transcript=transcript, user="eric-b"
            )
            kai_r = RecordingClient(client=kai_raw, transcript=transcript, user="kai")

            # Register both eric sessions
            await eric_a.call("plan", message="on laptop")
            await eric_b.call("plan", message="on desktop")

            # Name eric_a's session "dev-laptop"
            result = await eric_a.call("tty", name="dev-laptop")
            assert "dev-laptop" in result

            # kai talks to @eric:dev-laptop (friendly name, not hex ID)
            result = await kai_r.call(
                "talk", to="@eric:dev-laptop", message="hey laptop session"
            )
            assert "Talk session started" in result
            assert "@eric:dev-laptop" in result

            # eric_a (dev-laptop) should receive the message
            result_a = await eric_a.call("read_messages")
            assert "hey laptop session" in result_a

            # eric_b should NOT have the message
            result_b = await eric_b.call("read_messages")
            assert "No new messages" in result_b


class TestTalkSelfEcho:
    """Notification payload includes sender session key for self-echo rejection."""

    async def test_notification_carries_from_key(
        self,
        nats_server: str,
        shared_data_dir: Path,
        transcript: Transcript,
    ) -> None:
        """deliver() includes from_key in the NATS notification payload."""
        eric_cfg = BiffConfig(user="eric", repo_name=_TEST_REPO, relay_url=nats_server)
        eric_state = create_state(
            eric_cfg,
            shared_data_dir / "eric",
            tty="eeee1111",
            hostname="test",
            pwd="/test",
        )
        kai_cfg = BiffConfig(user="kai", repo_name=_TEST_REPO, relay_url=nats_server)
        kai_state = create_state(
            kai_cfg,
            shared_data_dir / "kai",
            tty="kkkk1111",
            hostname="test",
            pwd="/test",
        )

        eric_mcp = create_server(eric_state)
        kai_mcp = create_server(kai_state)

        # Subscribe to eric's talk notification subject before any messages
        nc = await nats_lib.connect(nats_server)  # pyright: ignore[reportUnknownMemberType]
        received: list[bytes] = []

        async def _capture(msg: object) -> None:
            received.append(msg.data)  # type: ignore[attr-defined]

        subject = f"biff.{_TEST_REPO}.talk.notify.eric"
        sub = await nc.subscribe(subject, cb=_capture)  # pyright: ignore[reportUnknownMemberType]

        try:
            async with (
                Client(FastMCPTransport(eric_mcp)) as eric_raw,
                Client(FastMCPTransport(kai_mcp)) as kai_raw,
            ):
                eric_r = RecordingClient(
                    client=eric_raw, transcript=transcript, user="eric"
                )
                kai_r = RecordingClient(
                    client=kai_raw, transcript=transcript, user="kai"
                )

                await eric_r.call("plan", message="available")

                # kai talks to eric — this triggers a notification
                await kai_r.call("talk", to="@eric", message="hello")
                await asyncio.sleep(0.3)

                # Notification should carry from_key = kai's session key
                assert len(received) >= 1
                data = json.loads(received[0])
                assert data["from"] == "kai"
                assert data["body"] == "hello"
                assert data["from_key"] == "kai:kkkk1111"
        finally:
            await sub.unsubscribe()  # pyright: ignore[reportUnknownMemberType]
            await nc.close()
