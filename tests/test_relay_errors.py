"""An unusable local relay directory is reported with its path, never a bare trace."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from biff.cli_session import cli_session
from biff.config import ResolvedConfig
from biff.models import BiffConfig
from biff.server.app import _heartbeat_loop
from tests._state import create_state

pytestmark = pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores directory modes"
)


@pytest.fixture
def blocked(tmp_path: Path) -> Iterator[Path]:
    """A relay directory that cannot be created: its parent is read-only."""
    parent = tmp_path / "locked"
    parent.mkdir()
    parent.chmod(0o500)
    yield parent / "relay-key"
    parent.chmod(0o700)


async def test_cli_reports_one_line_not_a_traceback(
    blocked: Path, tmp_path: Path
) -> None:
    resolved = ResolvedConfig(
        config=BiffConfig(user="kai", repo_name="r"),
        data_dir=blocked,
        repo_common_root=tmp_path,
    )
    with (
        patch("biff.cli_session.load_cli_config", return_value=resolved),
        pytest.raises(ValueError, match=str(blocked)) as caught,
    ):
        async with cli_session():
            pass
    assert "\n" not in str(caught.value)
    assert "Permission denied" in str(caught.value)
    assert "permissions" in str(caught.value)


async def test_heartbeat_warns_on_first_failure_and_on_recovery_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    state = create_state(
        BiffConfig(user="kai", repo_name="r"),
        tmp_path,
        tty="tty1",
        hostname="h",
        pwd="/",
    )
    outcomes = [OSError("disk"), OSError("disk"), None, OSError("disk"), None]
    stop = asyncio.Event()

    async def heartbeat(_key: str) -> None:
        outcome = outcomes.pop(0)
        if not outcomes:
            stop.set()
        if outcome is not None:
            raise outcome

    state.relay.heartbeat = AsyncMock(side_effect=heartbeat)  # type: ignore[method-assign]
    with caplog.at_level(logging.DEBUG, logger="biff.server.app"):
        await _heartbeat_loop(state, stop, interval=0.01)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [
        "Heartbeat failed",
        "Heartbeat recovered",
        "Heartbeat failed",
        "Heartbeat recovered",
    ]
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(debugs) == 1  # the repeated failure


async def test_mcp_client_sees_the_path_not_internal_server_error(
    blocked: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from fastmcp.client.transports import FastMCPTransport

    from biff.server.app import create_server
    from biff.testing.caller import StampedClient, caller_for

    state = create_state(
        BiffConfig(user="kai", repo_name="_test-blocked"),
        blocked,
        hostname="h",
        pwd="/p",
    )
    async with StampedClient(
        FastMCPTransport(create_server(state)), caller=caller_for("sess-blocked")
    ) as client:
        with (
            caplog.at_level(logging.ERROR, logger="biff.server.app"),
            pytest.raises(Exception, match=str(blocked)) as caught,
        ):
            await client.call_tool("who", {})
    assert "Internal server error" not in str(caught.value)
    logged = [r for r in caplog.records if str(blocked) in r.getMessage()]
    assert logged
    assert logged[0].exc_info is not None
