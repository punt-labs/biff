"""Fixtures for NatsRelay tests.

Provides a :class:`~biff.nats_relay.NatsRelay` connected to the shared
``nats_server`` fixture, with per-test cleanup of shared streams.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING

import pytest

from biff.nats_relay import NatsRelay

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_TEST_REPO = "_test-nats-unit"


@pytest.fixture
async def relay(nats_server: str) -> AsyncIterator[NatsRelay]:
    """Provide a connected NatsRelay and clean up after each test.

    Deletes shared streams entirely between tests for full isolation.
    WORK_QUEUE consumer state can interfere across tests if only
    messages are purged — deleting the stream removes consumers too.
    Test nats-server is disposable, so this is safe.
    """
    r = NatsRelay(url=nats_server, repo_name=_TEST_REPO)

    yield r

    # Aggressive cleanup: delete shared infrastructure.
    # purge_data() alone leaves consumers, which can interfere
    # with WORK_QUEUE delivery in subsequent tests.
    if r._nc is not None and r._js is not None:
        for name in (r._stream_name, r._wtmp_stream):
            with suppress(Exception):
                await r._js.delete_stream(name)
        with suppress(Exception):
            await r._js.delete_key_value(r._kv_bucket)  # pyright: ignore[reportUnknownMemberType]
    await r.close()


@pytest.fixture
async def second_relay(nats_server: str) -> AsyncIterator[NatsRelay]:
    """A second relay instance for cross-user tests."""
    r = NatsRelay(url=nats_server, repo_name=_TEST_REPO)
    yield r
    await r.close()
