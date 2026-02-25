"""End-to-end tests with two MCP servers sharing a NATS relay.

Mirrors test_subprocess/test_e2e_subprocess.py but with NatsRelay as the
backend instead of LocalRelay. Exercises the full tool stack:
FastMCPTransport -> FastMCP -> tool -> NatsRelay -> NATS <- other server.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import nats
import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport
from nats.js.errors import NotFoundError

from biff.models import Message
from biff.nats_relay import NatsRelay
from biff.testing import RecordingClient, Transcript

pytestmark = pytest.mark.nats


class TestCrossUserVisibility:
    """User A's presence changes are visible to User B across NATS."""

    @pytest.mark.transcript
    async def test_plan_visible_via_who(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """kai sets a plan; eric sees it via /who."""
        kai.transcript.title = "NATS E2E: plan visible via /who"
        kai.transcript.description = "Two MCP servers share presence through NATS KV."

        await kai.call("plan", message="refactoring the auth layer")
        result = await eric.call("who")

        assert "@kai" in result
        assert "refactoring the auth layer" in result

    @pytest.mark.transcript
    async def test_plan_visible_via_finger(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """kai sets a plan; eric checks via /finger."""
        kai.transcript.title = "NATS E2E: plan visible via /finger"
        kai.transcript.description = "Cross-server finger lookup over NATS KV."

        await kai.call("plan", message="debugging flaky test")
        result = await eric.call("finger", user="@kai")

        assert "Login: kai" in result
        assert "debugging flaky test" in result
        assert "Messages: on" in result

    @pytest.mark.transcript
    async def test_biff_off_visible_to_other(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """kai goes heads-down; eric sees 'messages off'."""
        kai.transcript.title = "NATS E2E: focus mode visible"
        kai.transcript.description = (
            "kai disables messages; eric sees the change across NATS."
        )

        await kai.call("plan", message="deep work on storage layer")
        await kai.call("mesg", enabled=False)
        result = await eric.call("finger", user="@kai")

        assert "Messages: off" in result
        assert "deep work on storage layer" in result


class TestMultiUserPresence:
    """Both NATS-backed servers active simultaneously."""

    @pytest.mark.transcript
    async def test_both_visible_in_who(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """Both users set plans; /who shows both."""
        kai.transcript.title = "NATS E2E: both visible in /who"
        kai.transcript.description = (
            "Two MCP servers set plans and verify mutual visibility over NATS."
        )

        await kai.call("plan", message="refactoring auth")
        await eric.call("plan", message="reviewing PRs")

        kai_sees = await kai.call("who")
        eric_sees = await eric.call("who")

        for result in (kai_sees, eric_sees):
            assert "@kai" in result
            assert "@eric" in result
            assert "refactoring auth" in result
            assert "reviewing PRs" in result

    @pytest.mark.transcript
    async def test_finger_each_other(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """Each user can /finger the other across NATS."""
        kai.transcript.title = "NATS E2E: finger each other"
        kai.transcript.description = (
            "Two MCP servers check each other's status via NATS."
        )

        await kai.call("plan", message="writing tests")
        await eric.call("plan", message="reviewing kai's PR")

        kai_checks = await kai.call("finger", user="@eric")
        eric_checks = await eric.call("finger", user="@kai")

        assert "reviewing kai's PR" in kai_checks
        assert "writing tests" in eric_checks


class TestPresenceLifecycle:
    """Full lifecycle over NATS: join, work, go dark, come back."""

    @pytest.mark.transcript
    async def test_full_presence_lifecycle(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """A full day of two teammates coordinating via NATS presence."""
        kai.transcript.title = "NATS E2E: presence lifecycle"
        kai.transcript.description = (
            "Full lifecycle over two MCP servers sharing NATS relay."
        )

        # Morning: both come online
        await kai.call("plan", message="starting on auth refactor")
        await eric.call("plan", message="triaging bug reports")

        # eric checks who's around
        who_result = await eric.call("who")
        assert "@kai" in who_result
        assert "@eric" in who_result

        # kai goes heads-down
        await kai.call("mesg", enabled=False)
        finger_result = await eric.call("finger", user="@kai")
        assert "Messages: off" in finger_result

        # kai finishes deep work, comes back
        await kai.call("mesg", enabled=True)
        await kai.call("plan", message="auth refactor done, reviewing PRs")

        # eric checks kai's new status
        finger_result = await eric.call("finger", user="@kai")
        assert "Messages: on" in finger_result
        assert "auth refactor done" in finger_result


class TestCrossRelayMessaging:
    """Message delivery across two MCP servers via NATS JetStream."""

    @pytest.mark.transcript
    async def test_send_and_check(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """kai sends a message; eric receives it across NATS."""
        kai.transcript.title = "NATS E2E: send and check messages"
        kai.transcript.description = (
            "Cross-server messaging via NATS JetStream POP semantics."
        )

        result = await kai.call("write", to="@eric", message="PR is ready")
        assert "@eric" in result

        result = await eric.call("read_messages")
        assert "kai" in result
        assert "PR is ready" in result

    @pytest.mark.transcript
    async def test_bidirectional_messaging(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """Both users exchange messages across NATS."""
        kai.transcript.title = "NATS E2E: bidirectional messaging"
        kai.transcript.description = (
            "Two MCP servers exchange messages through NATS JetStream."
        )

        await kai.call("write", to="eric", message="review my PR?")
        await eric.call("write", to="kai", message="sure, on it")

        kai_inbox = await kai.call("read_messages")
        eric_inbox = await eric.call("read_messages")

        assert "sure, on it" in kai_inbox
        assert "review my PR?" in eric_inbox

    @pytest.mark.transcript
    async def test_messages_consumed_on_read(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """Messages are consumed (POP semantics) — second read is empty."""
        kai.transcript.title = "NATS E2E: POP message semantics"
        kai.transcript.description = (
            "Messages are deleted on read — NATS WORK_QUEUE retention."
        )

        await kai.call("write", to="eric", message="auth module ready")

        # First read consumes the message
        result = await eric.call("read_messages")
        assert "auth module ready" in result

        # Second read is empty
        result = await eric.call("read_messages")
        assert "No new messages" in result


class TestTtyNameAutoAssign:
    """Lifespan auto-assigns tty_name to KV — verifiable via /who.

    Uses hex tty values (like production) to verify that /who shows the
    auto-assigned friendly name, NOT the raw hex.  The existing fixtures
    use tty="tty1"/"tty2" which masks the bug because tty_name and tty
    are the same string.
    """

    async def test_who_shows_friendly_name_not_hex(
        self,
        nats_server: str,
        shared_data_dir: Path,
        transcript: Transcript,
    ) -> None:
        """Lifespan auto-assigns ttyN; /who shows it instead of hex."""
        from biff.models import BiffConfig
        from biff.server.app import create_server
        from biff.server.state import create_state

        repo = "_test-nats-e2e"
        kai_cfg = BiffConfig(user="kai", repo_name=repo, relay_url=nats_server)
        kai_state = create_state(
            kai_cfg,
            shared_data_dir / "kai",
            tty="aabbccdd",
            hostname="test",
            pwd="/test",
        )
        eric_cfg = BiffConfig(user="eric", repo_name=repo, relay_url=nats_server)
        eric_state = create_state(
            eric_cfg,
            shared_data_dir / "eric",
            tty="11223344",
            hostname="test",
            pwd="/test",
        )

        kai_mcp = create_server(kai_state)
        eric_mcp = create_server(eric_state)

        async with (
            Client(FastMCPTransport(kai_mcp)) as kai_raw,
            Client(FastMCPTransport(eric_mcp)) as eric_raw,
        ):
            kai_r = RecordingClient(client=kai_raw, transcript=transcript, user="kai")
            RecordingClient(client=eric_raw, transcript=transcript, user="eric")

            # Both servers should have auto-assigned ttyN names.
            # /who reads from KV — should show friendly names.
            result = await kai_r.call("who")

            # Must show friendly names, NOT hex IDs
            assert "tty1" in result, f"Expected 'tty1' in /who output, got: {result}"
            assert "tty2" in result, f"Expected 'tty2' in /who output, got: {result}"
            # Must NOT show hex IDs
            assert "aabbccdd" not in result, (
                f"Hex ID 'aabbccdd' should not appear in /who: {result}"
            )
            assert "11223344" not in result, (
                f"Hex ID '11223344' should not appear in /who: {result}"
            )

    async def test_tty_name_survives_disconnect_reconnect(
        self,
        nats_server: str,
        shared_data_dir: Path,
        transcript: Transcript,
    ) -> None:
        """tty_name persists across disconnect/reconnect (nap cycle)."""
        from biff.models import BiffConfig
        from biff.nats_relay import NatsRelay
        from biff.server.app import create_server
        from biff.server.state import create_state

        repo = "_test-nats-e2e"
        kai_cfg = BiffConfig(user="kai", repo_name=repo, relay_url=nats_server)
        kai_state = create_state(
            kai_cfg,
            shared_data_dir / "kai",
            tty="ddee5566",
            hostname="test",
            pwd="/test",
        )
        kai_mcp = create_server(kai_state)

        async with Client(FastMCPTransport(kai_mcp)) as kai_raw:
            kai_r = RecordingClient(client=kai_raw, transcript=transcript, user="kai")

            # Verify tty_name is set after startup
            session = await kai_state.relay.get_session(kai_state.session_key)
            assert session is not None
            assert session.tty_name != "", f"Pre-disconnect: {session}"

            # Simulate nap: disconnect from NATS
            relay = kai_state.relay
            assert isinstance(relay, NatsRelay)
            await relay.disconnect()

            # Simulate wake: reconnect by calling a tool
            await kai_r.call("plan", message="back from nap")

            # Verify tty_name survived the cycle
            session = await kai_state.relay.get_session(kai_state.session_key)
            assert session is not None
            assert session.tty_name != "", (
                f"Post-reconnect: tty_name lost! Session: {session}"
            )

    async def test_tty_name_survives_heartbeat(
        self,
        nats_server: str,
        shared_data_dir: Path,
        transcript: Transcript,
    ) -> None:
        """tty_name persists after heartbeat updates last_active."""
        from biff.models import BiffConfig
        from biff.server.app import create_server
        from biff.server.state import create_state

        repo = "_test-nats-e2e"
        kai_cfg = BiffConfig(user="kai", repo_name=repo, relay_url=nats_server)
        kai_state = create_state(
            kai_cfg,
            shared_data_dir / "kai",
            tty="aabb7788",
            hostname="test",
            pwd="/test",
        )
        kai_mcp = create_server(kai_state)

        async with Client(FastMCPTransport(kai_mcp)) as kai_raw:
            kai_r = RecordingClient(client=kai_raw, transcript=transcript, user="kai")
            await kai_r.call("plan", message="testing")

            # Simulate heartbeat
            await kai_state.relay.heartbeat(kai_state.session_key)

            session = await kai_state.relay.get_session(kai_state.session_key)
            assert session is not None
            assert session.tty_name != "", (
                f"Post-heartbeat: tty_name lost! Session: {session}"
            )

    async def test_tty_name_survives_disconnect_then_heartbeat(
        self,
        nats_server: str,
        shared_data_dir: Path,
        transcript: Transcript,
    ) -> None:
        """tty_name persists after disconnect + heartbeat (POP cycle)."""
        from biff.models import BiffConfig
        from biff.nats_relay import NatsRelay
        from biff.server.app import create_server
        from biff.server.state import create_state

        repo = "_test-nats-e2e"
        kai_cfg = BiffConfig(user="kai", repo_name=repo, relay_url=nats_server)
        kai_state = create_state(
            kai_cfg,
            shared_data_dir / "kai",
            tty="cc991122",
            hostname="test",
            pwd="/test",
        )
        kai_mcp = create_server(kai_state)

        async with Client(FastMCPTransport(kai_mcp)) as kai_raw:
            kai_r = RecordingClient(client=kai_raw, transcript=transcript, user="kai")
            await kai_r.call("plan", message="testing")

            # Simulate full nap → POP cycle
            relay = kai_state.relay
            assert isinstance(relay, NatsRelay)
            await relay.disconnect()

            # POP cycle: reconnect via heartbeat
            await relay.heartbeat(kai_state.session_key)

            session = await relay.get_session(kai_state.session_key)
            assert session is not None
            assert session.tty_name != "", (
                f"Post-POP: tty_name lost! Session: {session}"
            )

            await relay.disconnect()

    async def test_kv_session_has_tty_name(
        self,
        nats_server: str,
        shared_data_dir: Path,
        transcript: Transcript,
    ) -> None:
        """Direct KV read confirms tty_name is persisted."""
        from biff.models import BiffConfig
        from biff.server.app import create_server
        from biff.server.state import create_state

        repo = "_test-nats-e2e"
        kai_cfg = BiffConfig(user="kai", repo_name=repo, relay_url=nats_server)
        kai_state = create_state(
            kai_cfg,
            shared_data_dir / "kai",
            tty="ffee1122",
            hostname="test",
            pwd="/test",
        )
        kai_mcp = create_server(kai_state)

        async with Client(FastMCPTransport(kai_mcp)) as kai_raw:
            kai_r = RecordingClient(client=kai_raw, transcript=transcript, user="kai")

            # Call any tool to ensure session is active
            await kai_r.call("plan", message="testing")

            # Read session directly from KV via the relay
            session = await kai_state.relay.get_session(kai_state.session_key)
            assert session is not None, "Session not found in KV"
            assert session.tty_name != "", (
                f"tty_name is empty in KV! Session: {session}"
            )
            assert session.tty_name.startswith("tty"), (
                f"tty_name should be auto-assigned ttyN, got: {session.tty_name!r}"
            )


class TestConsumerCleanup:
    """Durable consumers are deleted when sessions exit."""

    async def test_delete_session_removes_consumer(self, nats_server: str) -> None:
        """delete_session() deletes the per-session inbox consumer."""
        repo = "_test-consumer-cleanup"
        relay = NatsRelay(url=nats_server, repo_name=repo)
        session_key = "kai:tty1"
        consumer_name = relay._durable_name(session_key)

        try:
            # Deliver a message so the stream has data, then create
            # the durable consumer via pull_subscribe (not fetch(),
            # which deletes the consumer in its finally block).
            msg = Message(from_user="eric", to_user=session_key, body="hello")
            await relay.deliver(msg)

            js_relay, _ = await relay._ensure_connected()
            subject = relay._subject_for_key(session_key)
            sub = await js_relay.pull_subscribe(
                subject, durable=consumer_name, stream=relay._stream_name
            )
            await sub.unsubscribe()

            # Verify consumer exists on the shared stream.
            nc = await nats.connect(nats_server)  # pyright: ignore[reportUnknownMemberType]
            js = nc.jetstream()  # pyright: ignore[reportUnknownMemberType]
            info = await js.consumer_info(relay._stream_name, consumer_name)
            assert info.name == consumer_name

            # delete_session should remove both the KV entry and the consumer.
            await relay.delete_session(session_key)

            with pytest.raises(NotFoundError):
                await js.consumer_info(relay._stream_name, consumer_name)

            await nc.close()
        finally:
            await relay.delete_infrastructure()
            await relay.close()


class TestTalkSelfEchoFilter:
    """Self-echo rejection in the talk notification callback.

    The ``_on_talk_msg`` callback inside ``_manage_talk_subscription``
    compares ``from_key`` in the NATS notification payload against
    ``state.session_key`` and drops self-originated messages.
    """

    async def test_self_echo_rejected(
        self,
        nats_server: str,
        shared_data_dir: Path,
        transcript: Transcript,
    ) -> None:
        """Notification with from_key == session_key must NOT update talk message."""
        from biff.models import BiffConfig
        from biff.server.app import create_server
        from biff.server.state import create_state
        from biff.server.tools._descriptions import (
            _manage_talk_subscription,
            _reset_session,
            set_talk_partner,
        )

        _reset_session()
        repo = f"_test-selfecho-{id(self)}"
        cfg = BiffConfig(user="kai", repo_name=repo, relay_url=nats_server)
        state = create_state(
            cfg, shared_data_dir / "kai", tty="aa11bb22", hostname="test", pwd="/test"
        )
        mcp = create_server(state)

        async with Client(FastMCPTransport(mcp)) as kai_raw:
            RecordingClient(client=kai_raw, transcript=transcript, user="kai")

            # Set up talk subscription to "eric"
            set_talk_partner("eric")
            _, sub = await _manage_talk_subscription(state, None, None)
            assert sub is not None

            # Publish a talk notification FROM this session (self-echo)
            relay = state.relay
            assert isinstance(relay, NatsRelay)
            nc = await relay.get_nc()
            subject = relay.talk_notify_subject("kai")
            payload = json.dumps(
                {
                    "from": "kai",
                    "body": "self-echo should be ignored",
                    "from_key": state.session_key,
                }
            ).encode()
            await nc.publish(subject, payload)
            await asyncio.sleep(0.1)

            # Import _talk_message to check it was NOT set
            from biff.server.tools._descriptions import _talk_message

            assert _talk_message == "", (
                f"Self-echo was not rejected: _talk_message={_talk_message!r}"
            )

            # Clean up subscription
            await sub.unsubscribe()  # type: ignore[attr-defined]

        _reset_session()

    async def test_other_session_accepted(
        self,
        nats_server: str,
        shared_data_dir: Path,
        transcript: Transcript,
    ) -> None:
        """Notification with from_key != session_key updates talk message."""
        from biff.models import BiffConfig
        from biff.server.app import create_server
        from biff.server.state import create_state
        from biff.server.tools._descriptions import (
            _manage_talk_subscription,
            _reset_session,
            set_talk_partner,
        )

        _reset_session()
        repo = f"_test-selfecho-accept-{id(self)}"
        cfg = BiffConfig(user="kai", repo_name=repo, relay_url=nats_server)
        state = create_state(
            cfg, shared_data_dir / "kai", tty="cc33dd44", hostname="test", pwd="/test"
        )
        mcp = create_server(state)

        async with Client(FastMCPTransport(mcp)) as kai_raw:
            RecordingClient(client=kai_raw, transcript=transcript, user="kai")

            # Set up talk subscription — kai is talking to eric
            set_talk_partner("eric")
            _, sub = await _manage_talk_subscription(state, None, None)
            assert sub is not None

            # Publish a talk notification FROM eric's session (different key)
            relay = state.relay
            assert isinstance(relay, NatsRelay)
            nc = await relay.get_nc()
            subject = relay.talk_notify_subject("kai")
            payload = json.dumps(
                {
                    "from": "eric",
                    "body": "hello from eric",
                    "from_key": "eric:tty2",
                }
            ).encode()
            await nc.publish(subject, payload)
            await asyncio.sleep(0.1)

            from biff.server.tools._descriptions import _talk_message

            assert _talk_message == "@eric: hello from eric", (
                f"Expected talk message update, got: {_talk_message!r}"
            )

            await sub.unsubscribe()  # type: ignore[attr-defined]

        _reset_session()
