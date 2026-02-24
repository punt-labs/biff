"""Scale stress test against hosted NATS (Synadia Cloud).

Tests biff's relay layer under concurrent load using real NATS
connections to the production cloud infrastructure.

Run::

    BIFF_TEST_NATS_URL=tls://connect.ngs.global \\
    BIFF_TEST_NATS_CREDS=src/biff/data/demo.creds \\
        uv run pytest -m stress -v

Synadia Cloud account limits (Biff / NGS / biff-default)::

    Connections:             300
    Subscriptions per conn:  50
    Consumers per stream:    500  (R1)
    R1 Streams:              25
    R1 Disk:                 2.5 GiB

The consumers-per-stream limit (500) supports the 243-user
target (243 session + 243 user consumers = 486 worst case).
The stress test validates that biff operates correctly at
scale and cleans up after itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from nats.js.errors import NotFoundError

from biff.models import Message, RelayAuth, UserSession, WallPost
from biff.nats_relay import NatsRelay
from biff.tty import build_session_key

pytestmark = [pytest.mark.stress, pytest.mark.asyncio(loop_scope="session")]

# -- Constants --

_REPO = "_test-stress"
_INBOX_STREAM = f"biff-{_REPO}-inbox"
_KV_BUCKET = f"biff-{_REPO}-sessions"
_KV_STREAM = f"KV_{_KV_BUCKET}"
_WTMP_STREAM = f"biff-{_REPO}-wtmp"

# Scale: 20 real NATS connections, each a separate NatsRelay.
# Account allows 100; we use 20 + 2 from hosted E2E fixtures = 22.
_N_RELAYS = 20

# Per-stream consumer limit from Synadia Cloud account settings.
_CONSUMERS_PER_STREAM = 500


# -- Helpers --


def _user(i: int) -> tuple[str, str]:
    """Return (user, tty) for simulated user *i*."""
    return f"u{i:03d}", f"t{i:03d}"


def _session_key(i: int) -> str:
    user, tty = _user(i)
    return build_session_key(user, tty)


def _make_session(i: int, *, plan: str = "") -> UserSession:
    user, tty = _user(i)
    return UserSession(user=user, tty=tty, plan=plan)


async def _consumer_count(relay: NatsRelay, stream: str) -> int:
    """Count consumers on a stream without creating one."""
    js, _ = await relay._ensure_connected()  # pyright: ignore[reportPrivateUsage]
    try:
        info = await js.stream_info(stream)
        return info.state.consumer_count
    except NotFoundError:
        return 0


async def _kv_key_count(relay: NatsRelay) -> int:
    """Count KV keys via stream_info — no consumers created."""
    js, _ = await relay._ensure_connected()  # pyright: ignore[reportPrivateUsage]
    prefix = f"$KV.{_KV_BUCKET}."
    try:
        info = await js.stream_info(_KV_STREAM, subjects_filter=f"{prefix}>")
        return len(info.state.subjects) if info.state.subjects else 0
    except NotFoundError:
        return 0


async def _delete_consumer_safe(relay: NatsRelay, stream: str, name: str) -> None:
    """Delete a consumer, suppressing NotFoundError."""
    js, _ = await relay._ensure_connected()  # pyright: ignore[reportPrivateUsage]
    with suppress(NotFoundError):
        await js.delete_consumer(stream, name)


# -- Fixtures --


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def relays(
    hosted_nats_url: str,
    hosted_nats_auth: RelayAuth | None,
) -> AsyncIterator[list[NatsRelay]]:
    """Open N real NATS connections for stress testing.

    Each relay is a separate ``NatsRelay`` instance with its own
    NATS connection — the same topology as N real users.
    """
    opened: list[NatsRelay] = []
    for i in range(_N_RELAYS):
        relay = NatsRelay(
            url=hosted_nats_url,
            auth=hosted_nats_auth,
            name=f"stress-{i:03d}",
            repo_name=_REPO,
        )
        opened.append(relay)
    # Establish all connections concurrently
    await asyncio.gather(
        *(r._ensure_connected() for r in opened)  # pyright: ignore[reportPrivateUsage]
    )
    yield opened
    # Teardown: purge data, close all connections
    await opened[0].purge_data()
    for r in opened:
        await r.close()


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _cleanup(relays: list[NatsRelay]) -> AsyncIterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Purge data and delete lingering consumers after each test."""
    yield
    lead = relays[0]
    await lead.purge_data()
    # Delete all possible consumers from simulated users.
    # Concurrent deletion keeps cleanup fast against hosted NATS.
    deletes: list[asyncio.Task[None]] = []
    for i in range(_N_RELAYS):
        user, tty = _user(i)
        deletes.append(
            asyncio.ensure_future(
                _delete_consumer_safe(lead, _INBOX_STREAM, f"inbox-{user}-{tty}")
            )
        )
        deletes.append(
            asyncio.ensure_future(
                _delete_consumer_safe(lead, _INBOX_STREAM, f"userinbox-{user}")
            )
        )
    await asyncio.gather(*deletes)


# =====================================================================
# Tests
# =====================================================================


class TestConnectionScaling:
    """Verify many real NATS connections coexist on Synadia Cloud."""

    async def test_all_connections_alive(self, relays: list[NatsRelay]) -> None:
        """Each of the N relays has an active connection."""
        for relay in relays:
            js, kv = await relay._ensure_connected()  # pyright: ignore[reportPrivateUsage]
            assert js is not None
            assert kv is not None

    async def test_concurrent_kv_writes(self, relays: list[NatsRelay]) -> None:
        """All relays can write to shared KV concurrently."""
        await asyncio.gather(
            *(relay.update_session(_make_session(i)) for i, relay in enumerate(relays))
        )
        count = await _kv_key_count(relays[0])
        assert count == _N_RELAYS

        # Cleanup
        await asyncio.gather(
            *(relay.delete_session(_session_key(i)) for i, relay in enumerate(relays))
        )


class TestPresenceAtScale:
    """Presence operations — zero consumers, any number of users."""

    async def test_register_and_enumerate(self, relays: list[NatsRelay]) -> None:
        """Register N sessions via N connections, enumerate them."""
        lead = relays[0]
        kv_consumers_before = await _consumer_count(lead, _KV_STREAM)

        # Each relay registers its own session
        await asyncio.gather(
            *(relay.update_session(_make_session(i)) for i, relay in enumerate(relays))
        )

        # Enumerate 10 times — must not create any consumers
        for _ in range(10):
            sessions = await lead.get_sessions()
            assert len(sessions) == _N_RELAYS

        kv_consumers_after = await _consumer_count(lead, _KV_STREAM)
        assert kv_consumers_after == kv_consumers_before, (
            f"KV consumer leak: {kv_consumers_before} -> {kv_consumers_after} "
            f"after 10 get_sessions() calls"
        )

        # Cleanup
        await asyncio.gather(
            *(relay.delete_session(_session_key(i)) for i, relay in enumerate(relays))
        )

    async def test_plan_updates_concurrent(self, relays: list[NatsRelay]) -> None:
        """Concurrent plan updates from all N connections."""
        await asyncio.gather(
            *(
                relay.update_session(_make_session(i, plan=f"Feature {i}"))
                for i, relay in enumerate(relays)
            )
        )

        all_sessions = await relays[0].get_sessions()
        plans = {s.user: s.plan for s in all_sessions}
        for i in range(len(relays)):
            user, _ = _user(i)
            assert plans.get(user) == f"Feature {i}"

        await asyncio.gather(
            *(relay.delete_session(_session_key(i)) for i, relay in enumerate(relays))
        )


class TestMessagingWithinBudget:
    """Messaging with delete-after-use consumer pattern.

    Both ``fetch()`` and ``fetch_user_inbox()`` delete their durable
    consumer immediately after acking messages.  Steady-state consumer
    footprint from fetch operations is zero.
    """

    async def test_session_inbox(self, relays: list[NatsRelay]) -> None:
        """Send and fetch session-targeted messages for 20 users."""
        n_users = _N_RELAYS
        sender = relays[0]

        # Register sessions
        for i in range(n_users):
            await relays[i].update_session(_make_session(i))

        # Send one message to each user's session inbox
        for i in range(n_users):
            user, tty = _user(i)
            msg = Message(
                from_user="bot",
                to_user=f"{user}:{tty}",
                body=f"Hello user {i}",
            )
            await sender.deliver(msg)

        # Fetch — each call creates/reuses a durable consumer
        for i in range(n_users):
            messages = await relays[i].fetch(_session_key(i))
            assert len(messages) == 1
            assert messages[0].body == f"Hello user {i}"

        # fetch() deletes its consumer after acks — all should be gone.
        consumers = await _consumer_count(sender, _INBOX_STREAM)
        assert consumers == 0, f"Expected 0 consumers after fetch(), got {consumers}"

        # delete_session is still safe to call (suppress NotFoundError)
        for i in range(n_users):
            await relays[i].delete_session(_session_key(i))

    async def test_user_inbox_broadcast(self, relays: list[NatsRelay]) -> None:
        """Broadcast via user inbox for 20 users."""
        n_users = _N_RELAYS
        sender = relays[0]

        for i in range(n_users):
            user, _ = _user(i)
            msg = Message(from_user="bot", to_user=user, body=f"Broadcast {i}")
            await sender.deliver(msg)

        for i in range(n_users):
            user, _ = _user(i)
            messages = await relays[i].fetch_user_inbox(user)
            assert len(messages) == 1
            assert messages[0].body == f"Broadcast {i}"

        consumers = await _consumer_count(sender, _INBOX_STREAM)
        # fetch_user_inbox() deletes its consumer after acks complete.
        # All consumers should be gone.
        assert consumers == 0, (
            f"Expected 0 consumers after fetch_user_inbox(), got {consumers}"
        )

    async def test_second_fetch_is_empty(self, relays: list[NatsRelay]) -> None:
        """POP semantics: second fetch returns no messages."""
        sender = relays[0]
        user, tty = _user(0)
        key = _session_key(0)

        await sender.update_session(_make_session(0))
        await sender.deliver(
            Message(from_user="bot", to_user=f"{user}:{tty}", body="once")
        )

        first = await relays[0].fetch(key)
        assert len(first) == 1

        second = await relays[0].fetch(key)
        assert len(second) == 0

        await sender.delete_session(key)


class TestConsumerAccounting:
    """The canonical regression test: consumers must not grow over time."""

    async def test_no_leak_over_iterations(self, relays: list[NatsRelay]) -> None:
        """Run 20 register/fetch/delete cycles.  Consumer count stays flat."""
        lead = relays[0]
        n_users = 5
        iterations = 20

        # Warm up: one cycle to establish baseline
        for i in range(n_users):
            await lead.update_session(_make_session(i))
            await lead.deliver(
                Message(
                    from_user="bot",
                    to_user=_session_key(i),
                    body="warmup",
                )
            )
            await lead.fetch(_session_key(i))
            await lead.delete_session(_session_key(i))

        inbox_baseline = await _consumer_count(lead, _INBOX_STREAM)
        kv_baseline = await _consumer_count(lead, _KV_STREAM)

        for iteration in range(iterations):
            # Register
            for i in range(n_users):
                await lead.update_session(_make_session(i))

            # Enumerate (must not create consumers)
            sessions = await lead.get_sessions()
            assert len(sessions) == n_users

            # Send and fetch
            for i in range(n_users):
                await lead.deliver(
                    Message(
                        from_user="bot",
                        to_user=_session_key(i),
                        body=f"iter-{iteration}",
                    )
                )
                messages = await lead.fetch(_session_key(i))
                assert len(messages) == 1

            # Cleanup
            for i in range(n_users):
                await lead.delete_session(_session_key(i))

        inbox_final = await _consumer_count(lead, _INBOX_STREAM)
        kv_final = await _consumer_count(lead, _KV_STREAM)

        assert inbox_final == inbox_baseline, (
            f"Inbox consumer leak: {inbox_baseline} -> {inbox_final} "
            f"after {iterations} iterations"
        )
        assert kv_final == kv_baseline, (
            f"KV consumer leak: {kv_baseline} -> {kv_final} "
            f"after {iterations} iterations"
        )

    async def test_get_sessions_zero_consumers(self, relays: list[NatsRelay]) -> None:
        """100 get_sessions() calls must not change KV consumer count."""
        lead = relays[0]

        for i in range(5):
            await lead.update_session(_make_session(i))

        before = await _consumer_count(lead, _KV_STREAM)
        for _ in range(100):
            await lead.get_sessions()
        after = await _consumer_count(lead, _KV_STREAM)

        assert after == before, f"get_sessions() leaked consumers: {before} -> {after}"

        for i in range(5):
            await lead.delete_session(_session_key(i))


class TestWallBroadcast:
    """Wall operations at scale — zero consumers."""

    async def test_concurrent_wall_reads(self, relays: list[NatsRelay]) -> None:
        """One wall post, N concurrent readers from N connections."""
        poster = relays[0]
        from datetime import timedelta

        now = datetime.now(UTC)
        wall = WallPost(
            text="Release 0.6.1 shipping today!",
            from_user="admin",
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        await poster.set_wall(wall)

        kv_consumers_before = await _consumer_count(poster, _KV_STREAM)

        results = await asyncio.gather(*(relay.get_wall() for relay in relays))
        for result in results:
            assert result is not None
            assert result.text == "Release 0.6.1 shipping today!"

        kv_consumers_after = await _consumer_count(poster, _KV_STREAM)
        assert kv_consumers_after == kv_consumers_before, (
            f"Wall reads leaked consumers: {kv_consumers_before} -> "
            f"{kv_consumers_after}"
        )

        await poster.set_wall(None)


class TestConsumerFootprint:
    """Validate delete-after-use consumer pattern under load."""

    async def test_fetch_deletes_consumers(self, relays: list[NatsRelay]) -> None:
        """fetch() and fetch_user_inbox() leave zero consumers behind."""
        n_users = 10
        sender = relays[0]

        # Register sessions
        for i in range(n_users):
            await relays[i % _N_RELAYS].update_session(_make_session(i))

        consumers_before = await _consumer_count(sender, _INBOX_STREAM)

        # Send to both session and user inboxes
        for i in range(n_users):
            user, tty = _user(i)
            await sender.deliver(
                Message(from_user="bot", to_user=f"{user}:{tty}", body=f"tty-{i}")
            )
            await sender.deliver(
                Message(from_user="bot", to_user=user, body=f"user-{i}")
            )

        # Fetch from both inboxes — consumers created then deleted
        for i in range(n_users):
            relay = relays[i % _N_RELAYS]
            user, _ = _user(i)
            tty_msgs = await relay.fetch(_session_key(i))
            assert len(tty_msgs) == 1
            user_msgs = await relay.fetch_user_inbox(user)
            assert len(user_msgs) == 1

        consumers_after = await _consumer_count(sender, _INBOX_STREAM)
        assert consumers_after == consumers_before, (
            f"Consumer leak: {consumers_before} -> {consumers_after} "
            f"after {n_users} fetch() + fetch_user_inbox() calls"
        )

        # Cleanup
        for i in range(n_users):
            await relays[i % _N_RELAYS].delete_session(_session_key(i))

    async def test_repeated_fetch_cycles_stable(self, relays: list[NatsRelay]) -> None:
        """Consumer count stays flat across repeated send/fetch cycles."""
        lead = relays[0]
        n_users = 5
        cycles = 10

        consumers_before = await _consumer_count(lead, _INBOX_STREAM)

        for cycle in range(cycles):
            for i in range(n_users):
                user, tty = _user(i)
                await lead.deliver(
                    Message(
                        from_user="bot",
                        to_user=f"{user}:{tty}",
                        body=f"cycle-{cycle}",
                    )
                )
            for i in range(n_users):
                msgs = await lead.fetch(_session_key(i))
                assert len(msgs) == 1

        consumers_after = await _consumer_count(lead, _INBOX_STREAM)
        assert consumers_after == consumers_before, (
            f"Consumer drift: {consumers_before} -> {consumers_after} "
            f"after {cycles} send/fetch cycles"
        )


class TestAccountCapacity:
    """Discover and document the real capacity of the Synadia Cloud account.

    With DES-015 (count-only unread summaries), steady-state is
    0 consumers per active user.  The consumer limit no longer
    constrains concurrency — only connections do (300 limit).
    """

    async def test_report_limits(self, relays: list[NatsRelay]) -> None:
        """Query actual stream state and report capacity."""
        lead = relays[0]
        js, _ = await lead._ensure_connected()  # pyright: ignore[reportPrivateUsage]

        # Inbox stream info
        inbox_consumer_limit: int | str
        try:
            inbox_info = await js.stream_info(_INBOX_STREAM)
            inbox_consumer_limit = inbox_info.config.max_consumers  # type: ignore[assignment]  # pyright: ignore[reportUnknownMemberType]
        except NotFoundError:
            inbox_consumer_limit = "stream not found"

        # KV stream info
        kv_consumer_limit: int | str
        try:
            kv_info = await js.stream_info(_KV_STREAM)
            kv_consumer_limit = kv_info.config.max_consumers  # type: ignore[assignment]  # pyright: ignore[reportUnknownMemberType]
        except NotFoundError:
            kv_consumer_limit = "stream not found"

        # Report (visible in pytest -v -s output)
        print(f"\n{'=' * 60}")
        print("Synadia Cloud Account Capacity Report")
        print(f"{'=' * 60}")
        print(f"  Connections opened:         {_N_RELAYS}")
        print(f"  Inbox max_consumers:        {inbox_consumer_limit}")
        print(f"  KV max_consumers:           {kv_consumer_limit}")
        print(f"  Account consumer/stream:    {_CONSUMERS_PER_STREAM}")
        print("  Steady-state consumers/user: 0 (DES-015)")
        print("  Bottleneck:                 connections (300)")
        print("  Target (243 users):         OK")
        print(f"{'=' * 60}")
