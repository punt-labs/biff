"""Fixtures for hosted NATS E2E tests.

Connects to a real hosted NATS server (Synadia Cloud or self-hosted)
using credentials from environment variables:

    BIFF_TEST_NATS_URL        — Required. e.g. "tls://connect.ngs.global"
    BIFF_TEST_NATS_TOKEN      — Token auth
    BIFF_TEST_NATS_NKEYS_SEED — Path to NKey seed file
    BIFF_TEST_NATS_CREDS      — Path to NATS credentials file

At most one auth env var should be set.

Connection budget: hosted accounts often have low connection limits
(e.g. 5 per app on Synadia Cloud starter).  This module uses three
session-scoped connections (kai relay, eric relay, cleanup) and
reuses them across all tests.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Generator
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import nats
import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport

from biff.models import BiffConfig, RelayAuth
from biff.nats_relay import NatsRelay
from biff.server.app import create_server
from biff.server.state import create_state
from biff.testing import RecordingClient, Transcript

if TYPE_CHECKING:
    from nats.aio.client import Client as NatsClient

_TRANSCRIPT_DIR = Path(__file__).parent.parent / "transcripts"


def _relay_auth_from_env() -> RelayAuth | None:
    """Build RelayAuth from environment variables."""
    token = os.environ.get("BIFF_TEST_NATS_TOKEN", "")
    nkeys_seed = os.environ.get("BIFF_TEST_NATS_NKEYS_SEED", "")
    creds = os.environ.get("BIFF_TEST_NATS_CREDS", "")
    values = {
        k: v
        for k, v in [
            ("token", token),
            ("nkeys_seed", nkeys_seed),
            ("user_credentials", creds),
        ]
        if v
    }
    if len(values) > 1:
        names = ", ".join(sorted(values))
        pytest.fail(f"Multiple NATS auth env vars set: {names} — pick one")
    if not values:
        return None
    return RelayAuth(**values)


def _auth_connect_kwargs(auth: RelayAuth | None) -> dict[str, str]:
    """Build nats.connect() kwargs from RelayAuth."""
    if auth is None:
        return {}
    if auth.token:
        return {"token": auth.token}
    if auth.nkeys_seed:
        return {"nkeys_seed": auth.nkeys_seed}
    if auth.user_credentials:
        return {"user_credentials": auth.user_credentials}
    return {}


# -- Session-scoped fixtures (3 NATS connections total) --


@pytest.fixture(scope="session")
def hosted_nats_url() -> str:
    """NATS server URL from BIFF_TEST_NATS_URL env var."""
    url = os.environ.get("BIFF_TEST_NATS_URL", "")
    if not url:
        pytest.skip("BIFF_TEST_NATS_URL not set")
    return url


@pytest.fixture(scope="session")
def hosted_nats_auth() -> RelayAuth | None:
    """Authentication from BIFF_TEST_NATS_* env vars."""
    return _relay_auth_from_env()


@pytest.fixture(scope="session")
async def _cleanup_conn(  # pyright: ignore[reportUnusedFunction]
    hosted_nats_url: str, hosted_nats_auth: RelayAuth | None
) -> AsyncIterator[NatsClient]:
    """Session-scoped NATS connection for test cleanup."""
    nc: NatsClient = await nats.connect(  # pyright: ignore[reportUnknownMemberType]
        hosted_nats_url,
        name="biff-test-cleanup",
        **_auth_connect_kwargs(hosted_nats_auth),
    )
    yield nc
    await nc.close()


@pytest.fixture(scope="session")
async def kai_relay(
    hosted_nats_url: str, hosted_nats_auth: RelayAuth | None
) -> AsyncIterator[NatsRelay]:
    """Session-scoped NatsRelay for kai — one connection for all tests."""
    relay = NatsRelay(
        url=hosted_nats_url,
        auth=hosted_nats_auth,
        name="biff-test-kai",
    )
    yield relay
    await relay.close()


@pytest.fixture(scope="session")
async def eric_relay(
    hosted_nats_url: str, hosted_nats_auth: RelayAuth | None
) -> AsyncIterator[NatsRelay]:
    """Session-scoped NatsRelay for eric — one connection for all tests."""
    relay = NatsRelay(
        url=hosted_nats_url,
        auth=hosted_nats_auth,
        name="biff-test-eric",
    )
    yield relay
    await relay.close()


# -- Per-test fixtures --


@pytest.fixture(autouse=True)
async def _cleanup_nats(  # pyright: ignore[reportUnusedFunction]
    _cleanup_conn: NatsClient,
    kai_relay: NatsRelay,
    eric_relay: NatsRelay,
) -> AsyncIterator[None]:
    """Delete NATS infrastructure after each test for isolation.

    Also resets the relay handles so ``_ensure_connected()`` re-provisions
    the KV bucket and stream on the next test.
    """
    yield
    js = _cleanup_conn.jetstream()  # pyright: ignore[reportUnknownMemberType]
    with suppress(Exception):
        await js.delete_stream("BIFF_INBOX")
    with suppress(Exception):
        await js.delete_key_value("biff-sessions")  # pyright: ignore[reportUnknownMemberType]
    kai_relay.reset_infrastructure()
    eric_relay.reset_infrastructure()


@pytest.fixture
def shared_data_dir(tmp_path: Path) -> Path:
    """Shared data directory for unread state files."""
    return tmp_path


@pytest.fixture
def transcript(request: pytest.FixtureRequest) -> Generator[Transcript]:
    t = Transcript(title="")
    yield t
    node = cast("pytest.Item", request.node)  # pyright: ignore[reportUnknownMemberType]
    marker = node.get_closest_marker("transcript")
    if marker and t.entries:
        _TRANSCRIPT_DIR.mkdir(exist_ok=True)
        slug = node.name.replace("[", "_").replace("]", "")
        path = _TRANSCRIPT_DIR / f"{slug}.txt"
        path.write_text(t.render())


@pytest.fixture
async def kai_client(
    kai_relay: NatsRelay,
    shared_data_dir: Path,
) -> AsyncIterator[Client[Any]]:
    """MCP client for kai, reusing the session-scoped relay."""
    config = BiffConfig(user="kai")
    state = create_state(config, shared_data_dir / "kai", relay=kai_relay)
    mcp = create_server(state)
    async with Client(FastMCPTransport(mcp)) as client:
        yield client


@pytest.fixture
async def eric_client(
    eric_relay: NatsRelay,
    shared_data_dir: Path,
) -> AsyncIterator[Client[Any]]:
    """MCP client for eric, reusing the session-scoped relay."""
    config = BiffConfig(user="eric")
    state = create_state(config, shared_data_dir / "eric", relay=eric_relay)
    mcp = create_server(state)
    async with Client(FastMCPTransport(mcp)) as client:
        yield client


@pytest.fixture
async def kai(
    kai_client: Client[Any],
    transcript: Transcript,
) -> RecordingClient:
    """Recording client for kai in hosted NATS E2E tests."""
    return RecordingClient(client=kai_client, transcript=transcript, user="kai")


@pytest.fixture
async def eric(
    eric_client: Client[Any],
    transcript: Transcript,
) -> RecordingClient:
    """Recording client for eric in hosted NATS E2E tests."""
    return RecordingClient(client=eric_client, transcript=transcript, user="eric")
