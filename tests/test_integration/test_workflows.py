"""Multi-tool workflow integration tests with transcript capture.

Each test demonstrates a realistic biff session and captures a
transcript that can be rendered as demo output.
"""

from __future__ import annotations

import pytest

from biff.models import UserSession
from biff.server.state import ServerState
from biff.testing import RecordingClient


@pytest.mark.transcript
class TestSoloWorkflow:
    """A single engineer using biff for self-documentation."""

    async def test_plan_and_presence(
        self, recorder: RecordingClient, state: ServerState
    ) -> None:
        recorder.transcript.title = "Solo workflow: setting your plan"
        recorder.transcript.description = (
            "Set what you're working on, then check presence."
        )

        result = await recorder.call("plan", message="refactoring the auth layer")
        assert "refactoring" in result

        result = await recorder.call("who")
        assert "@kai" in result
        assert "refactoring" in result

        result = await recorder.call("finger", user="kai")
        assert "refactoring the auth layer" in result
        assert "accepting messages" in result


@pytest.mark.transcript
class TestTeamPresenceWorkflow:
    """Multiple engineers checking on each other."""

    async def test_team_presence(
        self, recorder: RecordingClient, state: ServerState
    ) -> None:
        recorder.transcript.title = "Team presence: who's online?"
        recorder.transcript.description = (
            "Check your team's status without interrupting anyone."
        )

        # Set up other team members
        state.sessions.update(UserSession(user="eric", plan="reviewing PR #42"))
        state.sessions.update(UserSession(user="priya", plan="debugging flaky test"))

        # kai sets their own plan
        await recorder.call("plan", message="writing integration tests")

        # Check who's online
        result = await recorder.call("who")
        assert "@kai" in result
        assert "@eric" in result
        assert "@priya" in result

        # Check on a specific teammate
        result = await recorder.call("finger", user="eric")
        assert "reviewing PR #42" in result


@pytest.mark.transcript
class TestAvailabilityWorkflow:
    """Controlling message reception for deep focus."""

    async def test_focus_mode(
        self, recorder: RecordingClient, state: ServerState
    ) -> None:
        recorder.transcript.title = "Focus mode: going heads-down"
        recorder.transcript.description = (
            "Turn off messages for deep work, then come back."
        )

        await recorder.call("plan", message="deep refactor of the storage layer")

        result = await recorder.call("biff", enabled=False)
        assert "off" in result

        # A teammate checking on us sees we're unavailable
        result = await recorder.call("finger", user="kai")
        assert "messages off" in result

        # Done with deep work, turn messages back on
        result = await recorder.call("biff", enabled=True)
        assert "on" in result

        result = await recorder.call("finger", user="kai")
        assert "accepting messages" in result
