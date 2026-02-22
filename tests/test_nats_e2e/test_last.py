"""NATS E2E tests for /last — wtmp stream and KV watcher.

Tests the full flow: login event on session creation, logout event
on session deletion, and /last tool returning formatted history.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress

import nats
import pytest

from biff.models import SessionEvent
from biff.nats_relay import NatsRelay
from biff.testing import RecordingClient

pytestmark = pytest.mark.nats

_TEST_REPO = "_test-nats-e2e"


@pytest.fixture(autouse=True)
async def _cleanup_wtmp(nats_server: str) -> AsyncIterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Delete wtmp stream after each test for isolation.

    Runs after the main _cleanup_nats fixture in conftest.py.
    """
    yield
    nc = await nats.connect(nats_server)  # pyright: ignore[reportUnknownMemberType]
    js = nc.jetstream()  # pyright: ignore[reportUnknownMemberType]
    with suppress(Exception):
        await js.delete_stream(f"biff-{_TEST_REPO}-wtmp")
    await nc.close()


class TestWtmpLoginEvent:
    """Login events appear on the wtmp stream after session creation."""

    async def test_login_event_created_on_startup(
        self, kai: RecordingClient, nats_server: str
    ) -> None:
        """Starting a server (via fixture) creates a login event."""
        # The fixture creates a server with kai's session.
        # The lifespan should have appended a login event.
        relay = NatsRelay(url=nats_server, repo_name=_TEST_REPO)
        try:
            events = await relay.get_wtmp(user="kai")
            assert len(events) >= 1
            login = next((e for e in events if e.event == "login"), None)
            assert login is not None
            assert login.user == "kai"
            assert login.session_key == "kai:tty1"
        finally:
            await relay.close()

    async def test_both_users_have_login_events(
        self, kai: RecordingClient, eric: RecordingClient, nats_server: str
    ) -> None:
        """Both servers create login events."""
        relay = NatsRelay(url=nats_server, repo_name=_TEST_REPO)
        try:
            events = await relay.get_wtmp()
            logins = [e for e in events if e.event == "login"]
            users = {e.user for e in logins}
            assert "kai" in users
            assert "eric" in users
        finally:
            await relay.close()


class TestWtmpStream:
    """Direct wtmp stream operations via NatsRelay."""

    async def test_append_and_get(self, nats_server: str) -> None:
        """Events appended to wtmp are retrievable."""
        relay = NatsRelay(url=nats_server, repo_name=_TEST_REPO)
        try:
            from datetime import UTC, datetime

            event = SessionEvent(
                session_key="test:tty1",
                event="login",
                user="test",
                tty="tty1",
                hostname="test-host",
                timestamp=datetime.now(UTC),
            )
            await relay.append_wtmp(event)
            events = await relay.get_wtmp(user="test")
            assert len(events) == 1
            assert events[0].user == "test"
            assert events[0].event == "login"
        finally:
            await relay.close()

    async def test_get_filtered_by_user(self, nats_server: str) -> None:
        """User filter returns only that user's events."""
        relay = NatsRelay(url=nats_server, repo_name=_TEST_REPO)
        try:
            from datetime import UTC, datetime

            now = datetime.now(UTC)
            for user in ("alice", "bob"):
                await relay.append_wtmp(
                    SessionEvent(
                        session_key=f"{user}:tty1",
                        event="login",
                        user=user,
                        tty="tty1",
                        timestamp=now,
                    )
                )
            events = await relay.get_wtmp(user="alice")
            assert all(e.user == "alice" for e in events)
            assert len(events) == 1
        finally:
            await relay.close()

    async def test_get_respects_count(self, nats_server: str) -> None:
        """Count parameter limits results."""
        relay = NatsRelay(url=nats_server, repo_name=_TEST_REPO)
        try:
            from datetime import UTC, datetime, timedelta

            now = datetime.now(UTC)
            for i in range(5):
                await relay.append_wtmp(
                    SessionEvent(
                        session_key=f"user{i}:tty1",
                        event="login",
                        user=f"user{i}",
                        tty="tty1",
                        timestamp=now - timedelta(minutes=i),
                    )
                )
            events = await relay.get_wtmp(count=3)
            assert len(events) == 3
        finally:
            await relay.close()

    async def test_events_sorted_most_recent_first(self, nats_server: str) -> None:
        """Events are returned most recent first."""
        relay = NatsRelay(url=nats_server, repo_name=_TEST_REPO)
        try:
            from datetime import UTC, datetime, timedelta

            now = datetime.now(UTC)
            # Append in chronological order
            for i in range(3):
                await relay.append_wtmp(
                    SessionEvent(
                        session_key=f"user{i}:tty1",
                        event="login",
                        user=f"user{i}",
                        tty="tty1",
                        timestamp=now - timedelta(minutes=3 - i),
                    )
                )
            events = await relay.get_wtmp()
            assert events[0].timestamp >= events[-1].timestamp
        finally:
            await relay.close()


class TestLastTool:
    """The /last tool returns formatted session history."""

    async def test_last_shows_login(self, kai: RecordingClient) -> None:
        """kai's login event appears in /last output."""
        # Allow a moment for the login event to be written
        await asyncio.sleep(0.5)
        result = await kai.call("last")
        assert "@kai" in result

    async def test_last_shows_both_users(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """Both users' login events appear in /last."""
        await asyncio.sleep(0.5)
        result = await kai.call("last")
        assert "@kai" in result
        assert "@eric" in result

    async def test_last_user_filter(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """User filter restricts results to that user."""
        await asyncio.sleep(0.5)
        result = await kai.call("last", user="@kai")
        assert "@kai" in result
        # eric should not appear when filtering for kai
        lines = result.strip().split("\n")
        data_lines = [ln for ln in lines if ln.strip().startswith("@")]
        assert all("@kai" in ln for ln in data_lines)

    async def test_last_shows_still_logged_in(self, kai: RecordingClient) -> None:
        """Active sessions show 'still logged in'."""
        await asyncio.sleep(0.5)
        result = await kai.call("last")
        assert "still logged in" in result
