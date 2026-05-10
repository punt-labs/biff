"""Active-session marker file tests (biff-dzqc).

``write_active_session`` drops a file under ``~/.punt-labs/biff/active/``
so the SessionEnd hook can find running sessions on shutdown.  Prior
to biff-dzqc the marker write was wrapped in ``with suppress(OSError)``
and silently skipped when it failed; the v1.8.0 defect on PID 983529
showed a companion KV row with no corresponding marker file.  These
tests pin the primary and companion marker invariants.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport

from biff.models import BiffConfig
from biff.server.app import create_server
from biff.server.state import CompanionSession, create_state


@pytest.fixture
def active_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin the active-marker directory to a tmp path for isolation."""
    root = tmp_path / "active"
    monkeypatch.setattr("biff.server.app.active_dir", lambda: root)
    return root


class TestActiveMarkers:
    """Lifespan startup writes marker files for primary and companion."""

    async def test_marker_for_primary(self, tmp_path: Path, active_root: Path) -> None:
        config = BiffConfig(
            user="kai",
            display_name="Kai",
            kind="human",
            repo_name="_test-markers",
        )
        state = create_state(
            config,
            tmp_path,
            tty="a1b2c3d4",
            hostname="test-host",
            pwd="/test",
        )
        mcp = create_server(state)

        async with Client(FastMCPTransport(mcp)):
            marker = active_root / "kai-a1b2c3d4"
            assert marker.exists(), "primary active marker missing"

    async def test_companion_marker_written_when_register_companion_runs(
        self, tmp_path: Path, active_root: Path
    ) -> None:
        """Companion marker is written by _register_companion (heartbeat path).

        Startup no longer registers the companion (biff-8fg3) -- the
        marker is written when the heartbeat path successfully resolves
        the roster and calls ``_register_companion``. This test pins
        that invariant by invoking the helper directly.
        """
        from biff.server.app import _register_companion

        config = BiffConfig(
            user="claude",
            display_name="Claude Agento",
            kind="agent",
            repo_name="_test-markers",
        )
        companion = CompanionSession(
            user="jfreeman",
            display_name="Jim Freeman",
            kind="human",
            tty="e5f6g7h8",
        )
        state = create_state(
            config,
            tmp_path,
            tty="a1b2c3d4",
            hostname="test-host",
            pwd="/test",
            companion=companion,
        )
        mcp = create_server(state)

        async with Client(FastMCPTransport(mcp)):
            companion_marker = active_root / "jfreeman-e5f6g7h8"
            assert not companion_marker.exists(), (
                "companion marker must not be written at startup"
            )
            await _register_companion(state)
            assert companion_marker.exists(), (
                "companion marker missing after _register_companion"
            )
            primary_marker = active_root / "claude-a1b2c3d4"
            assert primary_marker.exists(), "primary marker missing"

    async def test_marker_not_written_on_register_failure(
        self,
        tmp_path: Path,
        active_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed register_session must not leave an orphan marker behind.

        Pins the invariant "marker exists iff KV row exists".  If the KV
        write or TTY claim fails, the marker must also be absent so the
        SessionEnd hook never sees a session that has no KV row.
        """
        config = BiffConfig(
            user="kai",
            display_name="Kai",
            kind="human",
            repo_name="_test-markers",
        )
        state = create_state(
            config,
            tmp_path,
            tty="a1b2c3d4",
            hostname="test-host",
            pwd="/test",
        )

        async def _boom(*_args: object, **_kwargs: object) -> str:
            msg = "simulated claim failure"
            raise RuntimeError(msg)

        monkeypatch.setattr("biff.server.app.claim_tty_name", _boom)

        mcp = create_server(state)
        with pytest.raises(RuntimeError, match="simulated claim failure"):
            async with Client(FastMCPTransport(mcp)):
                pass

        marker = active_root / "kai-a1b2c3d4"
        assert not marker.exists(), (
            "primary marker must not exist when register_session fails"
        )
