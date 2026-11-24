"""Tests for the track_activity dormant-repo guard."""

from __future__ import annotations

from pathlib import Path

from biff._stdlib import enabled_marker_path
from biff.models import BiffConfig
from biff.server.tools._activity import _DISABLED_NOTICE, track_activity
from tests._state import create_state


class TestTrackActivity:
    """The decorator runs the wrapped body in an enabled repo."""

    async def test_runs_the_body_without_waking_the_poller(
        self, tmp_path: Path
    ) -> None:
        config = BiffConfig(user="kai", repo_name="test")
        state = create_state(config, tmp_path, repo_root=tmp_path)

        @track_activity(state)
        async def tool() -> str:
            return "ran"

        assert await tool() == "ran"
        # A tool call is not an arrival: it must not run a poller pass.
        assert not state.wake.is_set

    async def test_passes_arguments_through(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai", repo_name="test")
        state = create_state(config, tmp_path, repo_root=tmp_path)

        @track_activity(state)
        async def tool(message: str) -> str:
            return f"got {message}"

        assert await tool(message="hi") == "got hi"

    async def test_dormant_returns_notice_without_running(self, tmp_path: Path) -> None:
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
