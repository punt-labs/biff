"""Check the active-session marker invariant.

SessionEnd discovers running sessions through files under
~/.punt-labs/biff/active/. A registered session must have a marker;
failed registration must not leave an orphan marker.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp.client.transports import FastMCPTransport
from fastmcp.exceptions import ToolError

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
    """Lifespan startup writes marker files for the session."""

    async def test_marker_for_primary(self, tmp_path: Path, active_root: Path) -> None:
        config = BiffConfig(
            user="kai",
            display_name="Kai",
            kind="human",
            repo_name="_test-markers",
        )
        state = create_state(config, tmp_path, hostname="test-host", pwd="/test")
        mcp = create_server(state)

        async with StampedClient(FastMCPTransport(mcp)) as client:
            assert not list(active_root.glob("*")), "no marker before a caller"
            await client.call_tool("who", {})
            marker = active_root / state.session_key.replace(":", "-")
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
        state = create_state(config, tmp_path, hostname="test-host", pwd="/test")

        async def _boom(*_args: object, **_kwargs: object) -> str:
            msg = "simulated claim failure"
            raise RuntimeError(msg)

        monkeypatch.setattr("biff.session_registry.claim_tty_name", _boom)

        mcp = create_server(state)
        async with StampedClient(FastMCPTransport(mcp)) as client:
            with pytest.raises(ToolError, match="simulated claim failure"):
                await client.call_tool("who", {})

        assert not list(active_root.glob("*")), (
            "no marker may exist when register_session fails"
        )
