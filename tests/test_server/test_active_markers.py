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

    async def test_marker_for_companion(
        self, tmp_path: Path, active_root: Path
    ) -> None:
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
            primary_marker = active_root / "claude-a1b2c3d4"
            companion_marker = active_root / "jfreeman-e5f6g7h8"
            assert primary_marker.exists(), "primary marker missing"
            assert companion_marker.exists(), "companion marker missing"
