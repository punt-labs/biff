"""Helpers for the tests of a harness session's end: a seat, and relays that log."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, ClassVar, cast

import pytest

from biff.relay import LocalRelay
from tests.test_daemon.consuming import ConsumingRelay

if TYPE_CHECKING:
    from collections.abc import Callable

    from biff.client.connection import DaemonClient
    from biff.models import SessionEvent
    from tests.test_daemon.conftest import Harness

__all__ = [
    "ConsumingLogging",
    "LoggingRelay",
    "Seat",
    "logging_relay",
    "seat",
    "until",
]


class LoggingRelay(LocalRelay):
    """A local relay that keeps the session history it is given, as NATS does."""

    events: ClassVar[list[SessionEvent]] = []

    async def append_wtmp(self, event: SessionEvent) -> None:
        LoggingRelay.events.append(event)


class ConsumingLogging(ConsumingRelay):
    """A consuming relay that keeps the history it is given."""

    async def append_wtmp(self, event: SessionEvent) -> None:
        LoggingRelay.events.append(event)


@pytest.fixture(autouse=True)
def logging_relay(harness: Harness) -> None:
    """Make every relay of the harness keep the history it is given."""
    LoggingRelay.events = []
    harness.factory.relay_type = LoggingRelay


class Seat:
    """One harness session, and a second view of the relay it lives on."""

    def __init__(
        self, client: DaemonClient, viewer: LocalRelay, key: str, tty: str
    ) -> None:
        self.client = client
        self.viewer = viewer
        self.key = key
        self.tty = tty

    async def row_present(self) -> bool:
        return await self.viewer.get_session(self.key) is not None

    async def owner(self) -> str | None:
        return await self.viewer.get_tty_reservation_owner("kai", self.tty)


async def seat(harness: Harness, session_id: str, run: str = "a1") -> Seat:
    """Attach a harness client as session *session_id*, run *run*."""
    client = await harness.connect(session_id, "kai", client="mcp", main_agent_id=run)
    status = cast("dict[str, dict[str, str]]", await client.request("status"))
    viewer = LocalRelay(harness.factory.built[0])
    return Seat(
        client, viewer, status["data"]["session_key"], status["data"]["tty_name"]
    )


async def until(check: Callable[[], bool]) -> None:
    """Wait, without a fixed sleep, until *check* holds."""
    deadline = asyncio.get_running_loop().time() + 2.0
    while not check():
        assert asyncio.get_running_loop().time() < deadline, "never became true"
        await asyncio.sleep(0.02)
