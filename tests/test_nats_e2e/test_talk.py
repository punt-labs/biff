"""NATS E2E tests for talk — ephemeral real-time conversation over NATS.

Two MCP servers (kai and eric) backed by NatsRelay.  Talk is ephemeral
(BSD talk): frames ride NATS core pub/sub with no durable inbox.  The
receiving server holds the frame in its shared TalkState (fed by the
always-on subscription); the model surfaces it via ``talk_read``.  These
tests exercise the invite -> read -> accept -> message -> end round-trip.
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
from biff.testing import RecordingClient, Transcript

pytestmark = pytest.mark.nats

_TEST_REPO = "_test-nats-e2e"


class TestTalkInitiation:
    """Starting a talk session sends an ephemeral invite."""

    async def test_talk_sends_invite(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """kai inviting eric returns an invite confirmation."""
        await eric.call("plan", message="available")
        result = await kai.call("talk", to="@eric:tty2", message="hey, review my PR?")
        assert "Invite sent" in result
        assert "eric" in result

    async def test_talk_offline_user(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """Talk to an offline user returns error."""
        result = await kai.call("talk", to="@nobody")
        assert "not online" in result

    async def test_bare_user_needs_session(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """A bare @user (no tty) is rejected — talk is session-scoped."""
        await eric.call("plan", message="available")
        result = await kai.call("talk", to="@eric")
        assert "specific session" in result

    async def test_self_talk_rejected(self, kai: RecordingClient) -> None:
        """Talking to your own session is refused."""
        await kai.call("plan", message="available")
        result = await kai.call("talk", to="@kai:tty1")
        assert "your own session" in result


class TestTalkReceive:
    """The invited agent surfaces the invite and messages via talk_read."""

    async def test_read_surfaces_invite(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """After kai invites eric, eric's talk_read shows who wants to talk."""
        await eric.call("plan", message="available")
        await kai.call("talk", to="@eric:tty2", message="are you there?")
        await asyncio.sleep(0.3)  # let eric's subscription receive the frame

        result = await eric.call("talk_read")
        assert "kai" in result
        assert "wants to talk" in result

    async def test_read_no_activity(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """talk_read with nothing held returns the idle sentinel."""
        await eric.call("plan", message="available")
        result = await eric.call("talk_read")
        assert "No pending talk activity" in result

    async def test_listen_wakes_on_invite(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """talk_listen wakes when an invite frame arrives."""
        await eric.call("plan", message="available")
        await kai.call("plan", message="available")

        listen_task = asyncio.create_task(eric.call("talk_listen", timeout=10))
        await asyncio.sleep(0.3)
        await kai.call("talk", to="@eric:tty2", message="urgent: deploy broken")

        result = await asyncio.wait_for(listen_task, timeout=5.0)
        assert "kai" in result
        assert "wants to talk" in result

    async def test_listen_timeout(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """talk_listen returns the idle sentinel when nothing arrives."""
        await eric.call("plan", message="available")
        result = await eric.call("talk_listen", timeout=1)
        assert "No pending talk activity" in result


class TestTalkEnd:
    """Ending a talk session."""

    async def test_end_after_invite(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """talk_end closes an outstanding invite (inviting phase)."""
        await eric.call("plan", message="available")
        await kai.call("talk", to="@eric:tty2")
        result = await kai.call("talk_end")
        assert "Talk session with eric ended" in result

    async def test_end_no_session(self, kai: RecordingClient) -> None:
        """talk_end with no active session returns error."""
        result = await kai.call("talk_end")
        assert "No active talk session" in result


class TestTalkConversation:
    """Full ephemeral talk conversation flow."""

    @pytest.mark.transcript
    async def test_full_talk_flow(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """Complete conversation: invite, accept, exchange, end."""
        kai.transcript.title = "NATS E2E: talk conversation"
        kai.transcript.description = (
            "Ephemeral real-time conversation between two MCP servers via NATS."
        )

        await kai.call("plan", message="implementing talk")
        await eric.call("plan", message="reviewing PRs")

        # kai invites eric.
        result = await kai.call("talk", to="@eric:tty2", message="review PR #42?")
        assert "Invite sent" in result

        # eric sees the invite and accepts by talking back.
        await asyncio.sleep(0.3)
        read = await eric.call("talk_read")
        assert "kai" in read
        result = await eric.call("talk", to="@kai:tty1", message="sure, looking now")
        assert "accepted their invite" in result

        # kai sees the acceptance/opening message and replies.
        await asyncio.sleep(0.3)
        read = await kai.call("talk_read")
        assert "sure, looking now" in read
        result = await kai.call("talk", to="@eric:tty2", message="thanks!")
        assert "Sent to eric:tty2" in result

        # eric sees the reply.
        await asyncio.sleep(0.3)
        read = await eric.call("talk_read")
        assert "thanks!" in read

        # kai ends the session.
        result = await kai.call("talk_end")
        assert "ended" in result


class TestTalkTtyNameResolution:
    """Talk resolves a friendly tty_name to the correct session (session-scoped)."""

    async def test_invite_reaches_named_session_only(
        self,
        nats_server: str,
        shared_data_dir: Path,
        transcript: Transcript,
    ) -> None:
        """An invite to @eric:dev-laptop reaches tty_a's TalkState, not tty_b's."""
        eric_cfg = BiffConfig(user="eric", repo_name=_TEST_REPO, relay_url=nats_server)
        eric_a_state = create_state(
            eric_cfg, shared_data_dir / "eric-a", tty="aaaa1111", hostname="t", pwd="/t"
        )
        eric_b_state = create_state(
            eric_cfg, shared_data_dir / "eric-b", tty="bbbb2222", hostname="t", pwd="/t"
        )
        kai_cfg = BiffConfig(user="kai", repo_name=_TEST_REPO, relay_url=nats_server)
        kai_state = create_state(
            kai_cfg, shared_data_dir / "kai", tty="cccc3333", hostname="t", pwd="/t"
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

            await eric_a.call("plan", message="on laptop")
            await eric_b.call("plan", message="on desktop")

            result = await eric_a.call("tty", name="dev-laptop")
            assert "dev-laptop" in result

            result = await kai_r.call(
                "talk", to="@eric:dev-laptop", message="hey laptop session"
            )
            assert "Invite sent" in result
            assert "eric:dev-laptop" in result

            await asyncio.sleep(0.3)
            result_a = await eric_a.call("talk_read")
            assert "kai" in result_a

            result_b = await eric_b.call("talk_read")
            assert "No pending talk activity" in result_b


class TestTalkInviteFrame:
    """The invite frame carries session-scoped routing metadata."""

    async def test_invite_frame_carries_keys(
        self,
        nats_server: str,
        shared_data_dir: Path,
        transcript: Transcript,
    ) -> None:
        """talk publishes an invite frame with from_key and to_key (DES-043)."""
        eric_cfg = BiffConfig(user="eric", repo_name=_TEST_REPO, relay_url=nats_server)
        eric_state = create_state(
            eric_cfg, shared_data_dir / "eric", tty="eeee1111", hostname="t", pwd="/t"
        )
        kai_cfg = BiffConfig(user="kai", repo_name=_TEST_REPO, relay_url=nats_server)
        kai_state = create_state(
            kai_cfg, shared_data_dir / "kai", tty="kkkk1111", hostname="t", pwd="/t"
        )

        eric_mcp = create_server(eric_state)
        kai_mcp = create_server(kai_state)

        nc = await nats_lib.connect(nats_server)  # pyright: ignore[reportUnknownMemberType]
        received: list[bytes] = []

        async def _capture(msg: object) -> None:
            received.append(msg.data)  # type: ignore[attr-defined]

        subject = "biff.talk.notify.eric:eeee1111"
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
                await kai_r.call("talk", to="@eric:eeee1111", message="hello")
                await asyncio.sleep(0.3)

                assert len(received) >= 1
                frames = [json.loads(r) for r in received]
                invite = next(f for f in frames if f.get("type") == "invite")
                assert invite["from"] == "kai"
                assert invite["body"] == "hello"
                assert invite["from_key"] == "kai:kkkk1111"
                assert invite["to_key"] == "eric:eeee1111"
        finally:
            await sub.unsubscribe()  # pyright: ignore[reportUnknownMemberType]
            await nc.close()
