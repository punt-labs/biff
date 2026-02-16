"""End-to-end presence tests over real subprocesses.

Mirrors test_integration/test_e2e_presence.py but with actual ``biff serve``
subprocesses communicating over stdio pipes. Exercises the full stack:
CLI -> Typer -> FastMCP -> tool -> LocalRelay -> filesystem <- other process.
"""

from __future__ import annotations

import pytest

from biff.testing import RecordingClient

pytestmark = pytest.mark.subprocess


class TestCrossUserVisibility:
    """User A's presence changes are visible to User B across processes."""

    @pytest.mark.transcript
    async def test_plan_visible_via_who(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """kai sets a plan; eric sees it via /who."""
        kai.transcript.title = "Subprocess: plan visible via /who"
        kai.transcript.description = (
            "Two biff subprocesses share state through the filesystem."
        )

        await kai.call("plan", message="refactoring the auth layer")
        result = await eric.call("who")

        assert "@kai" in result
        assert "refactoring the auth layer" in result

    @pytest.mark.transcript
    async def test_plan_visible_via_finger(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """kai sets a plan; eric checks via /finger."""
        kai.transcript.title = "Subprocess: plan visible via /finger"
        kai.transcript.description = "Cross-process finger lookup over stdio transport."

        await kai.call("plan", message="debugging flaky test")
        result = await eric.call("finger", user="@kai")

        assert "Login: kai" in result
        assert "debugging flaky test" in result
        assert "messages on" in result

    @pytest.mark.transcript
    async def test_biff_off_visible_to_other(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """kai goes heads-down; eric sees 'messages off'."""
        kai.transcript.title = "Subprocess: focus mode visible"
        kai.transcript.description = (
            "kai disables messages; eric sees the change across processes."
        )

        await kai.call("plan", message="deep work on storage layer")
        await kai.call("biff", enabled=False)
        result = await eric.call("finger", user="@kai")

        assert "messages off" in result
        assert "deep work on storage layer" in result


class TestMultiUserPresence:
    """Both subprocess users active simultaneously."""

    @pytest.mark.transcript
    async def test_both_visible_in_who(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """Both users set plans; /who shows both."""
        kai.transcript.title = "Subprocess: both visible in /who"
        kai.transcript.description = (
            "Two subprocesses set plans and verify mutual visibility."
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
        """Each user can /finger the other across processes."""
        kai.transcript.title = "Subprocess: finger each other"
        kai.transcript.description = "Two subprocesses check each other's status."

        await kai.call("plan", message="writing tests")
        await eric.call("plan", message="reviewing kai's PR")

        kai_checks = await kai.call("finger", user="@eric")
        eric_checks = await eric.call("finger", user="@kai")

        assert "reviewing kai's PR" in kai_checks
        assert "writing tests" in eric_checks


class TestPresenceLifecycle:
    """Full lifecycle over subprocesses: join, work, go dark, come back."""

    @pytest.mark.transcript
    async def test_full_presence_lifecycle(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """A full day of two teammates coordinating via subprocess presence."""
        kai.transcript.title = "Subprocess: presence lifecycle"
        kai.transcript.description = (
            "Full lifecycle over real subprocesses and stdio transport."
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
        assert "messages on" in finger_result
        assert "auth refactor done" in finger_result


class TestCrossProcessMessaging:
    """Message delivery across subprocesses via shared filesystem."""

    @pytest.mark.transcript
    async def test_send_and_check(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """kai sends a message; eric receives it across processes."""
        kai.transcript.title = "Subprocess: send and check messages"
        kai.transcript.description = (
            "Cross-process messaging over real stdio subprocesses."
        )

        result = await kai.call("send_message", to="@eric", message="PR is ready")
        assert "@eric" in result

        result = await eric.call("check_messages")
        assert "From kai" in result
        assert "PR is ready" in result

    @pytest.mark.transcript
    async def test_bidirectional_messaging(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """Both users exchange messages across processes."""
        kai.transcript.title = "Subprocess: bidirectional messaging"
        kai.transcript.description = (
            "Two subprocesses exchange messages through shared filesystem."
        )

        await kai.call("send_message", to="eric", message="review my PR?")
        await eric.call("send_message", to="kai", message="sure, on it")

        kai_inbox = await kai.call("check_messages")
        eric_inbox = await eric.call("check_messages")

        assert "sure, on it" in kai_inbox
        assert "review my PR?" in eric_inbox
