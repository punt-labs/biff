"""Active-session marker file tests.

``write_active_session`` drops a file under ``~/.punt-labs/biff/active/``
so the SessionEnd hook can find running sessions on shutdown.  The
marker write used to be wrapped in ``with suppress(OSError)``
and silently skipped when it failed; a v1.8.0 defect on PID 983529
showed a companion KV row with no corresponding marker file.  These
tests pin the primary and companion marker invariants.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp.client.transports import FastMCPTransport

from biff.models import BiffConfig
from biff.server.app import create_server
from biff.server.state import create_state
from biff.testing.caller import StampedClient


@pytest.fixture
def active_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin the active-marker directory to a tmp path for isolation."""
    root = tmp_path / "active"
    monkeypatch.setattr("biff.session_registry.active_dir", lambda: root)
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

        async with StampedClient(FastMCPTransport(mcp)):
            marker = active_root / "kai-a1b2c3d4"
            assert marker.exists(), "primary active marker missing"

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

        monkeypatch.setattr("biff.session_registry.claim_tty_name", _boom)

        mcp = create_server(state)
        with pytest.raises(RuntimeError, match="simulated claim failure"):
            async with StampedClient(FastMCPTransport(mcp)):
                pass

        marker = active_root / "kai-a1b2c3d4"
        assert not marker.exists(), (
            "primary marker must not exist when register_session fails"
        )
