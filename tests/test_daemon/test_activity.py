"""A request counts as activity for the session that made it, except a poll."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import pytest

from biff.models import Message
from biff.relay import LocalRelay
from biff.session_routing import SessionRouting

if TYPE_CHECKING:
    from datetime import datetime

    from biff.client.connection import DaemonClient
    from tests.test_daemon.conftest import Harness

_KEY = SessionRouting("s-active").session_key("kai")


async def _last_tool_at(harness: Harness) -> datetime:
    row = await LocalRelay(harness.factory.built[0]).get_session(_KEY)
    assert row is not None
    return row.last_tool_at


async def _attached(harness: Harness) -> tuple[DaemonClient, datetime]:
    """A client of an agent session, and the activity time the attach left."""
    client = await harness.connect("s-active", "kai", client="mcp")
    await asyncio.sleep(0.05)
    return client, await _last_tool_at(harness)


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("who", {}),
        ("finger", {"user": "kai"}),
        ("last", {}),
        ("write", {"to": "kai", "message": "hi"}),
    ],
)
async def test_a_request_counts_as_activity(
    harness: Harness, method: str, params: dict[str, str]
) -> None:
    client, before = await _attached(harness)

    await client.request(method, **params)

    assert await _last_tool_at(harness) > before


@pytest.mark.parametrize("method", ["read", "status"])
async def test_a_poll_or_a_status_check_does_not(harness: Harness, method: str) -> None:
    client, before = await _attached(harness)

    await client.request(method)

    assert await _last_tool_at(harness) == before


async def test_a_read_that_finds_mail_does(harness: Harness) -> None:
    client, before = await _attached(harness)
    relay = LocalRelay(harness.factory.built[0])
    await relay.deliver(Message(from_user="eric", to_user=_KEY, body="hi"))

    result = cast("dict[str, object]", await client.request("read"))

    assert (bool(result["data"]), await _last_tool_at(harness) > before) == (True, True)
