"""End-to-end tests with two MCP clients sharing state.

Each test exercises the full protocol path for both users:
Client A -> FastMCPTransport -> Server A -> Store <- Server B <- Client B

The shared data directory simulates two users in the same git repo.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from biff.testing import RecordingClient

if TYPE_CHECKING:
    from fastmcp import Client


async def _check_description(client: Client[Any]) -> str:
    """Get the check_messages tool description from an MCP client."""
    tools = await client.list_tools()
    for tool in tools:
        if tool.name == "check_messages":
            assert tool.description is not None
            return tool.description
    raise AssertionError("check_messages tool not found")


class TestCrossUserVisibility:
    """User A's presence changes are visible to User B."""

    @pytest.mark.transcript
    async def test_plan_visible_via_who(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """kai sets a plan; eric sees it via /who."""
        kai.transcript.title = "Cross-user: plan visible via /who"
        kai.transcript.description = "kai sets a plan, eric checks who's online."

        await kai.call("plan", message="refactoring the auth layer")
        result = await eric.call("who")

        assert "@kai" in result
        assert "refactoring the auth layer" in result

    @pytest.mark.transcript
    async def test_plan_visible_via_finger(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """kai sets a plan; eric checks via /finger."""
        kai.transcript.title = "Cross-user: plan visible via /finger"
        kai.transcript.description = "kai sets a plan, eric checks kai's status."

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
        kai.transcript.title = "Cross-user: focus mode visible"
        kai.transcript.description = "kai goes heads-down, eric sees the status change."

        await kai.call("plan", message="deep work on storage layer")
        await kai.call("biff", enabled=False)
        result = await eric.call("finger", user="@kai")

        assert "Messages: off" in result
        assert "deep work on storage layer" in result


class TestMultiUserPresence:
    """Both users active simultaneously."""

    @pytest.mark.transcript
    async def test_both_visible_in_who(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """Both users set plans; /who shows both."""
        kai.transcript.title = "Multi-user: both visible in /who"
        kai.transcript.description = "Two teammates set plans and check who's online."

        await kai.call("plan", message="refactoring auth")
        await eric.call("plan", message="reviewing PRs")

        kai_sees = await kai.call("who")
        eric_sees = await eric.call("who")

        # Both users see both plans
        for result in (kai_sees, eric_sees):
            assert "@kai" in result
            assert "@eric" in result
            assert "refactoring auth" in result
            assert "reviewing PRs" in result

    @pytest.mark.transcript
    async def test_finger_each_other(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """Each user can /finger the other."""
        kai.transcript.title = "Multi-user: finger each other"
        kai.transcript.description = "Two teammates check each other's status."

        await kai.call("plan", message="writing tests")
        await eric.call("plan", message="reviewing kai's PR")

        kai_checks = await kai.call("finger", user="@eric")
        eric_checks = await eric.call("finger", user="@kai")

        assert "reviewing kai's PR" in kai_checks
        assert "writing tests" in eric_checks


class TestPresenceLifecycle:
    """Full lifecycle: join, work, go dark, come back."""

    @pytest.mark.transcript
    async def test_full_presence_lifecycle(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """A full day of two teammates coordinating via presence."""
        kai.transcript.title = "Presence lifecycle: a day of coordination"
        kai.transcript.description = (
            "kai and eric coordinate work through presence tools."
        )

        # Morning: both come online
        await kai.call("plan", message="starting on auth refactor")
        await eric.call("plan", message="triaging bug reports")

        # eric checks who's around
        who_result = await eric.call("who")
        assert "@kai" in who_result
        assert "@eric" in who_result

        # kai goes heads-down
        await kai.call("biff", enabled=False)
        finger_result = await eric.call("finger", user="@kai")
        assert "Messages: off" in finger_result

        # kai finishes deep work, comes back
        await kai.call("biff", enabled=True)
        await kai.call("plan", message="auth refactor done, reviewing PRs")

        # eric checks kai's new status
        finger_result = await eric.call("finger", user="@kai")
        assert "Messages: on" in finger_result
        assert "auth refactor done" in finger_result


class TestCrossUserMessaging:
    """User A sends a message; User B receives it."""

    @pytest.mark.transcript
    async def test_send_and_check(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """kai sends a message; eric checks and sees it."""
        kai.transcript.title = "Cross-user: send and check messages"
        kai.transcript.description = "kai sends a message, eric checks inbox."

        result = await kai.call("send_message", to="@eric", message="PR #42 is ready")
        assert "@eric" in result

        result = await eric.call("check_messages")
        assert "From kai" in result
        assert "PR #42 is ready" in result

    @pytest.mark.transcript
    async def test_messages_marked_read(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """Checked messages don't appear again."""
        kai.transcript.title = "Cross-user: messages marked read"
        kai.transcript.description = "eric checks messages, second check is empty."

        await kai.call("send_message", to="eric", message="first message")
        await eric.call("check_messages")

        result = await eric.call("check_messages")
        assert "No new messages" in result

    @pytest.mark.transcript
    async def test_bidirectional_messaging(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """Both users can send and receive."""
        kai.transcript.title = "Cross-user: bidirectional messaging"
        kai.transcript.description = "kai and eric exchange messages."

        await kai.call("send_message", to="eric", message="review my PR?")
        await eric.call("send_message", to="kai", message="sure, on it")

        kai_inbox = await kai.call("check_messages")
        eric_inbox = await eric.call("check_messages")

        assert "From eric" in kai_inbox
        assert "sure, on it" in kai_inbox
        assert "From kai" in eric_inbox
        assert "review my PR?" in eric_inbox

    @pytest.mark.transcript
    async def test_deliver_when_biff_off(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """Messages queue even when recipient has biff off."""
        kai.transcript.title = "Cross-user: deliver when biff off"
        kai.transcript.description = (
            "eric turns biff off, kai sends anyway, eric checks later."
        )

        await eric.call("biff", enabled=False)
        await kai.call("send_message", to="eric", message="urgent fix needed")

        # eric turns biff back on and checks
        await eric.call("biff", enabled=True)
        result = await eric.call("check_messages")
        assert "From kai" in result
        assert "urgent fix needed" in result


class TestCrossUserDynamicDescriptions:
    """check_messages description reflects unread state across users."""

    async def test_description_updates_after_incoming_message(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """eric sends kai a message; kai's next tool call updates description."""
        await eric.call("send_message", to="kai", message="auth module ready")
        # kai calls any tool — triggers description refresh
        await kai.call("plan", message="working")
        desc = await _check_description(kai.client)
        assert "1 unread" in desc
        assert "@eric" in desc

    async def test_description_reverts_after_check(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """After checking messages, description reverts to base."""
        await eric.call("send_message", to="kai", message="hello")
        await kai.call("plan", message="working")
        desc = await _check_description(kai.client)
        assert "1 unread" in desc
        # Check clears unread
        await kai.call("check_messages")
        desc = await _check_description(kai.client)
        assert "unread" not in desc

    async def test_sender_description_unaffected(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """Sending a message doesn't add unread to sender's description."""
        await kai.call("send_message", to="eric", message="hey")
        desc = await _check_description(kai.client)
        assert "unread" not in desc
