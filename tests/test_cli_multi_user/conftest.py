"""Fixtures for tier 2b CLI multi-user tests.

Two ``cli_session()`` instances sharing a local NATS server.
Each session gets a real NatsRelay connection with KV presence,
JetStream messaging, and wtmp events — the same code path as
the interactive REPL, but without stdin threads or display.

Uses ``@pytest.mark.nats`` — requires local ``nats-server``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

import nats
import pytest

from biff.cli_session import CliContext, cli_session
from biff.config import ResolvedConfig
from biff.models import BiffConfig

_TEST_REPO = "_test-cli-multi"

pytestmark = pytest.mark.nats


def _make_resolved(user: str, nats_url: str, tmp_path: Path) -> ResolvedConfig:
    """Build a ResolvedConfig for a test user."""
    return ResolvedConfig(
        config=BiffConfig(
            user=user,
            repo_name=_TEST_REPO,
            relay_url=nats_url,
        ),
        data_dir=tmp_path / user,
        repo_root=tmp_path,
    )


@pytest.fixture(autouse=True)
async def _cleanup_nats(nats_server: str) -> AsyncIterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Delete shared NATS streams after each test for full isolation."""
    yield
    nc = await nats.connect(nats_server)  # pyright: ignore[reportUnknownMemberType]
    js = nc.jetstream()  # pyright: ignore[reportUnknownMemberType]
    for prefix in ("biff", "biff-dev"):
        with suppress(Exception):
            await js.delete_stream(f"{prefix}-inbox")
        with suppress(Exception):
            await js.delete_stream(f"{prefix}-wtmp")
        with suppress(Exception):
            await js.delete_key_value(f"{prefix}-sessions")  # pyright: ignore[reportUnknownMemberType]
    await nc.close()


@pytest.fixture()
async def kai(nats_server: str, tmp_path: Path) -> AsyncIterator[CliContext]:
    """CLI session for user kai backed by local NATS."""
    resolved = _make_resolved("kai", nats_server, tmp_path)
    with patch("biff.cli_session.load_config", return_value=resolved):
        async with cli_session() as ctx:
            yield ctx


@pytest.fixture()
async def eric(nats_server: str, tmp_path: Path) -> AsyncIterator[CliContext]:
    """CLI session for user eric backed by local NATS."""
    resolved = _make_resolved("eric", nats_server, tmp_path)
    with patch("biff.cli_session.load_config", return_value=resolved):
        async with cli_session() as ctx:
            yield ctx
