"""Fixtures for NatsRelay tests.

Provides a :class:`~biff.nats_relay.NatsRelay` connected to the shared
``nats_server`` fixture, with per-test cleanup of streams and KV buckets.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING

import pytest

from biff.nats_relay import NatsRelay

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
async def relay(nats_server: str) -> AsyncIterator[NatsRelay]:
    """Provide a connected NatsRelay and clean up after each test.

    Purges all streams and KV buckets between tests for isolation.
    """
    r = NatsRelay(url=nats_server)

    yield r

    # Clean up: delete stream (removes durable consumers) and KV bucket.
    # close() resets all cached state (_nc, _js, _kv) to None.
    if r._nc is not None and r._js is not None:
        with suppress(Exception):
            await r._js.delete_stream("BIFF_INBOX")
        with suppress(Exception):
            await r._js.delete_key_value("biff-sessions")  # pyright: ignore[reportUnknownMemberType]

    await r.close()


@pytest.fixture
async def second_relay(nats_server: str) -> AsyncIterator[NatsRelay]:
    """A second relay instance for cross-user tests."""
    r = NatsRelay(url=nats_server)
    yield r
    await r.close()
