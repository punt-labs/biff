"""Tests for the track_activity tool decorator."""

from __future__ import annotations

from pathlib import Path

from biff._stdlib import enabled_marker_path
from biff.models import BiffConfig
from biff.server.state import create_state
from biff.server.tools._activity import _DISABLED_NOTICE, track_activity


class TestTrackActivity:
    """The decorator records a tool call and runs the wrapped body."""

    async def test_touches_activity_and_runs(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai", repo_name="test")
        state = create_state(config, tmp_path, repo_root=tmp_path)
        state.activity.enter_nap()
        assert state.activity.napping is True

        @track_activity(state)
        async def tool() -> str:
            return "ran"

        result = await tool()
        assert result == "ran"
        # touch() clears napping so the poller resumes active ticks.
        assert state.activity.napping is False

    async def test_passes_arguments_through(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai", repo_name="test")
        state = create_state(config, tmp_path, repo_root=tmp_path)

        @track_activity(state)
        async def tool(message: str) -> str:
            return f"got {message}"

        assert await tool(message="hi") == "got hi"

    async def test_dormant_returns_notice_without_running(
        self, tmp_path: Path
    ) -> None:
        """A disabled repo returns the actionable notice, not the body."""
        config = BiffConfig(user="kai", repo_name="test")
        state = create_state(config, tmp_path, dormant=True, repo_root=tmp_path)
        ran = False

        @track_activity(state)
        async def tool() -> str:
            nonlocal ran
            ran = True
            return "ran"

        result = await tool()
        assert result == _DISABLED_NOTICE
        assert ran is False

    async def test_dormant_writes_no_marker(self, tmp_path: Path) -> None:
        """The notice never enables biff -- no marker is written."""
        config = BiffConfig(user="kai", repo_name="test")
        state = create_state(config, tmp_path, dormant=True, repo_root=tmp_path)

        @track_activity(state)
        async def tool() -> str:
            return "ran"

        await tool()
        assert not enabled_marker_path(tmp_path).exists()
