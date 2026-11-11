"""A signal arriving before any caller declared a session cleans up nothing."""

from __future__ import annotations

import signal
from pathlib import Path

import pytest
from fastmcp.client.transports import FastMCPTransport

from biff.models import BiffConfig
from biff.server.app import create_server
from biff.server.state import ServerState, create_state
from biff.session_routing import SessionRouting
from biff.testing.caller import StampedClient


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ServerState:
    monkeypatch.setattr("biff._stdlib.biff_data_dir", lambda: tmp_path / "biff")

    def _sentinel_dir(repo: str) -> Path:
        return tmp_path / repo

    monkeypatch.setattr("biff.server.app.sentinel_dir", _sentinel_dir)
    return create_state(
        BiffConfig(user="kai", repo_name="sigrepo"),
        tmp_path / "data",
        hostname="h",
        pwd="/p",
        unread_dir=tmp_path / "unread",
    )


async def _sentinels_after_sigterm(
    state: ServerState, sentinels: Path, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """Run the lifespan's SIGTERM handler without killing pytest.

    Read inside the lifespan: normal teardown removes the sentinel again.
    """

    def _no_kill(_pid: int, _sig: int) -> None:
        return None

    monkeypatch.setattr("biff.server.app.os.kill", _no_kill)
    async with StampedClient(FastMCPTransport(create_server(state))):
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        return (
            [p.read_text() for p in sentinels.glob("*")] if sentinels.exists() else []
        )


async def test_unbound_server_writes_no_sentinel(
    state: ServerState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert not state.is_bound
    assert (
        await _sentinels_after_sigterm(state, tmp_path / "sigrepo", monkeypatch) == []
    )


async def test_bound_server_writes_its_sentinel(
    state: ServerState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    routing = SessionRouting("sess-1")
    state.bind(routing)
    assert await _sentinels_after_sigterm(state, tmp_path / "sigrepo", monkeypatch) == [
        routing.session_key("kai")
    ]
