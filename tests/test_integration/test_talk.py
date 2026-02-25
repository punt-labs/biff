"""Integration tests for talk tools on LocalRelay (error path).

Talk requires NATS. These tests verify the error messages are
returned cleanly when using the LocalRelay (filesystem) backend.
"""

from __future__ import annotations

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
