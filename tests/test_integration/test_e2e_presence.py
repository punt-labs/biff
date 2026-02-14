"""End-to-end presence tests with two MCP clients sharing state.

Each test exercises the full protocol path for both users:
Client A -> FastMCPTransport -> Server A -> SessionStore <- Server B <- Client B

The shared data directory simulates two users in the same git repo.
"""

from __future__ import annotations

import pytest

from biff.testing import RecordingClient


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

        assert "@kai" in result
        assert "debugging flaky test" in result
        assert "accepting messages" in result

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

        assert "messages off" in result
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
        assert "messages off" in finger_result

        # kai finishes deep work, comes back
        await kai.call("biff", enabled=True)
        await kai.call("plan", message="auth refactor done, reviewing PRs")

        # eric checks kai's new status
        finger_result = await eric.call("finger", user="@kai")
        assert "accepting messages" in finger_result
        assert "auth refactor done" in finger_result
