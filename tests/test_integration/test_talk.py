"""Integration tests for talk tools on LocalRelay (error path).

Talk requires NATS. These tests verify the error messages are
returned cleanly when using the LocalRelay (filesystem) backend.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from biff.server.state import ServerState
from biff.server.tools._session import update_current_session
from biff.testing import RecordingClient


class TestTalkRequiresNats:
    """Talk tools return clear errors on LocalRelay."""

    async def test_talk_requires_nats(self, recorder: RecordingClient) -> None:
        result = await recorder.call("talk", to="@eric", message="hello")
        assert "NATS relay" in result

    async def test_talk_listen_requires_nats(self, recorder: RecordingClient) -> None:
        result = await recorder.call("talk_listen", timeout=1)
        assert "NATS relay" in result

    async def test_talk_end_no_session(self, recorder: RecordingClient) -> None:
        result = await recorder.call("talk_end")
        assert "No active talk session" in result

    async def test_talk_read_requires_nats(self, recorder: RecordingClient) -> None:
        result = await recorder.call("talk_read")
        assert "NATS relay" in result


class TestTalkEndRefreshesActivity:
    """talk_end must count as activity, like every other track_activity tool.

    Unlike talk/talk_read/talk_listen, talk_end never called
    ``update_current_session`` -- ending a talk didn't reset the idle
    time shown in ``/who``/``/finger``, even though it is genuine user
    activity (biff-liu round 2).
    """

    async def test_talk_end_advances_last_tool_at(
        self, recorder: RecordingClient, state: ServerState
    ) -> None:
        await update_current_session(state)  # real activity baseline
        session = await state.relay.get_session(state.session_key)
        assert session is not None
        backdated = datetime.now(UTC) - timedelta(minutes=10)
        await state.relay.update_session(
            session.model_copy(
                update={"last_active": backdated, "last_tool_at": backdated}
            )
        )

        # The baseline ``update_current_session`` call above already moved
        # ``last_tool_at`` past ``backdated``, so asserting only
        # ``> backdated`` would pass even if talk_end's own
        # ``update_current_session`` call were deleted -- it would prove
        # nothing about the fix under test. Capture ``before`` immediately
        # before the call under test and require ``last_tool_at`` to have
        # advanced past THAT point.
        before = datetime.now(UTC)
        await recorder.call("talk_end")

        session = await state.relay.get_session(state.session_key)
        assert session is not None
        assert session.last_tool_at >= before
