"""Integration tests for /last tool via RecordingClient.

Uses the LocalRelay which stubs out wtmp (returns empty list).
These tests verify the tool is registered and handles the empty case.
Full wtmp testing requires NATS E2E tests.
"""

from __future__ import annotations

from biff.testing import RecordingClient


class TestLastToolIntegration:
    async def test_last_returns_no_history(self, kai: RecordingClient) -> None:
        """LocalRelay returns empty wtmp — tool reports no history."""
        result = await kai.call("last")
        assert "No session history" in result

    async def test_last_with_user_filter(self, kai: RecordingClient) -> None:
        """User filter still returns no history on LocalRelay."""
        result = await kai.call("last", user="@kai")
        assert "No session history" in result

    async def test_last_with_count(self, kai: RecordingClient) -> None:
        """Count parameter accepted even on LocalRelay."""
        result = await kai.call("last", count=5)
        assert "No session history" in result
