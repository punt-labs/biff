"""End-to-end tests with two MCP servers sharing a NATS relay.

Mirrors test_subprocess/test_e2e_subprocess.py but with NatsRelay as the
backend instead of LocalRelay. Exercises the full tool stack:
FastMCPTransport -> FastMCP -> tool -> NatsRelay -> NATS <- other server.
"""

from __future__ import annotations

import pytest

from biff.testing import RecordingClient

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
        assert "From kai" in result
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
