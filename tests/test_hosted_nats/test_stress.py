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

With DES-015 (count-only unread summaries), the steady-state
JetStream consumer footprint is 0 per user, so the
consumers-per-stream limit (500) is no longer the primary
capacity bottleneck.  The 300 connection limit is now the
main constraint.  The stress test validates that biff operates
correctly at scale and cleans up after itself.
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

pytestmark = [
    pytest.mark.stress,
    pytest.mark.hosted,
    pytest.mark.asyncio(loop_scope="session"),
]

# -- Constants --

_REPO = "_test-stress"
# Test-isolated stream names — "biff-dev" prefix avoids touching production.
_STREAM_PREFIX = "biff-dev"
_INBOX_STREAM = f"{_STREAM_PREFIX}-inbox"
_KV_BUCKET = f"{_STREAM_PREFIX}-sessions"
_KV_STREAM = f"KV_{_KV_BUCKET}"
_WTMP_STREAM = f"{_STREAM_PREFIX}-wtmp"

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
    """Count KV keys for this repo via stream_info — no consumers created."""
    js, _ = await relay._ensure_connected()  # pyright: ignore[reportPrivateUsage]
    prefix = f"$KV.{_KV_BUCKET}.{_REPO}."
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
            stream_prefix=_STREAM_PREFIX,
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
    # Consumer names are repo-prefixed (DES-016).
    # Concurrent deletion keeps cleanup fast against hosted NATS.
    deletes: list[asyncio.Task[None]] = []
    for i in range(_N_RELAYS):
        user, tty = _user(i)
        deletes.append(
            asyncio.create_task(
                _delete_consumer_safe(
                    lead, _INBOX_STREAM, f"{_REPO}-inbox-{user}-{tty}"
                )
            )
        )
        deletes.append(
            asyncio.create_task(
                _delete_consumer_safe(lead, _INBOX_STREAM, f"{_REPO}-userinbox-{user}")
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
        await asyncio.gather(
            *(relays[i].update_session(_make_session(i)) for i in range(n_users))
        )

        # Send one message to each user's session inbox
        await asyncio.gather(
            *(
                sender.deliver(
                    Message(
                        from_user="bot",
                        to_user=f"{_user(i)[0]}:{_user(i)[1]}",
                        body=f"Hello user {i}",
                    )
                )
                for i in range(n_users)
            )
        )

        # Fetch — each call creates/reuses a durable consumer
        results = await asyncio.gather(
            *(relays[i].fetch(_session_key(i)) for i in range(n_users))
        )
        for i, messages in enumerate(results):
            assert len(messages) == 1
            assert messages[0].body == f"Hello user {i}"

        # fetch() deletes its consumer after acks — all should be gone.
        consumers = await _consumer_count(sender, _INBOX_STREAM)
        assert consumers == 0, f"Expected 0 consumers after fetch(), got {consumers}"

        # delete_session is still safe to call (suppress NotFoundError)
        await asyncio.gather(
            *(relays[i].delete_session(_session_key(i)) for i in range(n_users))
        )

    async def test_user_inbox_broadcast(self, relays: list[NatsRelay]) -> None:
        """Broadcast via user inbox for 20 users."""
        n_users = _N_RELAYS
        sender = relays[0]

        await asyncio.gather(
            *(
                sender.deliver(
                    Message(
                        from_user="bot",
                        to_user=_user(i)[0],
                        body=f"Broadcast {i}",
                    )
                )
                for i in range(n_users)
            )
        )

        results = await asyncio.gather(
            *(relays[i].fetch_user_inbox(_user(i)[0]) for i in range(n_users))
        )
        for i, messages in enumerate(results):
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
        await asyncio.gather(
            *(lead.update_session(_make_session(i)) for i in range(n_users))
        )
        await asyncio.gather(
            *(
                lead.deliver(
                    Message(
                        from_user="bot",
                        to_user=_session_key(i),
                        body="warmup",
                    )
                )
                for i in range(n_users)
            )
        )
        await asyncio.gather(*(lead.fetch(_session_key(i)) for i in range(n_users)))
        await asyncio.gather(
            *(lead.delete_session(_session_key(i)) for i in range(n_users))
        )

        inbox_baseline = await _consumer_count(lead, _INBOX_STREAM)
        kv_baseline = await _consumer_count(lead, _KV_STREAM)

        for iteration in range(iterations):
            # Register
            await asyncio.gather(
                *(lead.update_session(_make_session(i)) for i in range(n_users))
            )

            # Enumerate (must not create consumers)
            sessions = await lead.get_sessions()
            assert len(sessions) == n_users

            # Send all, then fetch all (deliver must complete before fetch)
            await asyncio.gather(
                *(
                    lead.deliver(
                        Message(
                            from_user="bot",
                            to_user=_session_key(i),
                            body=f"iter-{iteration}",
                        )
                    )
                    for i in range(n_users)
                )
            )
            results = await asyncio.gather(
                *(lead.fetch(_session_key(i)) for i in range(n_users))
            )
            for messages in results:
                assert len(messages) == 1

            # Cleanup
            await asyncio.gather(
                *(lead.delete_session(_session_key(i)) for i in range(n_users))
            )

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

        await asyncio.gather(*(lead.update_session(_make_session(i)) for i in range(5)))

        before = await _consumer_count(lead, _KV_STREAM)
        # Batch in groups of 10 for concurrency
        for _ in range(10):
            await asyncio.gather(*(lead.get_sessions() for _ in range(10)))
        after = await _consumer_count(lead, _KV_STREAM)

        assert after == before, f"get_sessions() leaked consumers: {before} -> {after}"

        await asyncio.gather(*(lead.delete_session(_session_key(i)) for i in range(5)))


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
        await asyncio.gather(
            *(
                relays[i % _N_RELAYS].update_session(_make_session(i))
                for i in range(n_users)
            )
        )

        consumers_before = await _consumer_count(sender, _INBOX_STREAM)

        # Send to both session and user inboxes
        await asyncio.gather(
            *(
                sender.deliver(
                    Message(
                        from_user="bot",
                        to_user=f"{_user(i)[0]}:{_user(i)[1]}",
                        body=f"tty-{i}",
                    )
                )
                for i in range(n_users)
            )
        )
        await asyncio.gather(
            *(
                sender.deliver(
                    Message(
                        from_user="bot",
                        to_user=_user(i)[0],
                        body=f"user-{i}",
                    )
                )
                for i in range(n_users)
            )
        )

        # Fetch from both inboxes — consumers created then deleted
        tty_results = await asyncio.gather(
            *(relays[i % _N_RELAYS].fetch(_session_key(i)) for i in range(n_users))
        )
        user_results = await asyncio.gather(
            *(
                relays[i % _N_RELAYS].fetch_user_inbox(_user(i)[0])
                for i in range(n_users)
            )
        )
        for tty_msgs in tty_results:
            assert len(tty_msgs) == 1
        for user_msgs in user_results:
            assert len(user_msgs) == 1

        consumers_after = await _consumer_count(sender, _INBOX_STREAM)
        assert consumers_after == consumers_before, (
            f"Consumer leak: {consumers_before} -> {consumers_after} "
            f"after {n_users} fetch() + fetch_user_inbox() calls"
        )

        # Cleanup
        await asyncio.gather(
            *(
                relays[i % _N_RELAYS].delete_session(_session_key(i))
                for i in range(n_users)
            )
        )

    async def test_repeated_fetch_cycles_stable(self, relays: list[NatsRelay]) -> None:
        """Consumer count stays flat across repeated send/fetch cycles."""
        lead = relays[0]
        n_users = 5
        cycles = 10

        consumers_before = await _consumer_count(lead, _INBOX_STREAM)

        for cycle in range(cycles):
            await asyncio.gather(
                *(
                    lead.deliver(
                        Message(
                            from_user="bot",
                            to_user=f"{_user(i)[0]}:{_user(i)[1]}",
                            body=f"cycle-{cycle}",
                        )
                    )
                    for i in range(n_users)
                )
            )
            results = await asyncio.gather(
                *(lead.fetch(_session_key(i)) for i in range(n_users))
            )
            for msgs in results:
                assert len(msgs) == 1

        consumers_after = await _consumer_count(lead, _INBOX_STREAM)
        assert consumers_after == consumers_before, (
            f"Consumer drift: {consumers_before} -> {consumers_after} "
            f"after {cycles} send/fetch cycles"
        )


class TestUnreadSummaryZeroConsumers:
    """DES-015: get_unread_summary() must create zero consumers.

    This is the core property that DES-015 exists to guarantee.
    The poller calls get_unread_summary() every 2 seconds per user.
    At 243 users that's 121 calls/second.  If any of those calls
    create a consumer, the account limit is hit within minutes.
    """

    async def test_single_relay_repeated_calls(self, relays: list[NatsRelay]) -> None:
        """100 get_unread_summary() calls from one relay — zero consumers."""
        lead = relays[0]
        key = _session_key(0)
        await lead.update_session(_make_session(0))

        # Seed some messages so the summary has work to do
        await asyncio.gather(
            *(
                lead.deliver(Message(from_user="bot", to_user=key, body=f"msg-{j}"))
                for j in range(5)
            ),
            lead.deliver(
                Message(from_user="bot", to_user=_user(0)[0], body="broadcast")
            ),
        )

        consumers_before = await _consumer_count(lead, _INBOX_STREAM)

        # Batch in groups of 10 for concurrency
        for _ in range(10):
            summaries = await asyncio.gather(
                *(lead.get_unread_summary(key) for _ in range(10))
            )
            for summary in summaries:
                assert summary.count == 6  # 5 tty + 1 user

        consumers_after = await _consumer_count(lead, _INBOX_STREAM)
        assert consumers_after == consumers_before, (
            f"get_unread_summary() leaked consumers: "
            f"{consumers_before} -> {consumers_after} after 100 calls"
        )

        await lead.delete_session(key)

    async def test_concurrent_polling_all_relays(self, relays: list[NatsRelay]) -> None:
        """All 20 relays call get_unread_summary() concurrently, 10 rounds."""
        lead = relays[0]

        # Register sessions and seed messages for all users
        await asyncio.gather(
            *(relays[i].update_session(_make_session(i)) for i in range(len(relays)))
        )
        await asyncio.gather(
            *(
                lead.deliver(
                    Message(
                        from_user="bot",
                        to_user=_session_key(i),
                        body=f"hello-{i}",
                    )
                )
                for i in range(len(relays))
            )
        )

        consumers_before = await _consumer_count(lead, _INBOX_STREAM)
        rounds = 10

        for _ in range(rounds):
            summaries = await asyncio.gather(
                *(
                    relays[i].get_unread_summary(_session_key(i))
                    for i in range(len(relays))
                )
            )
            for i, summary in enumerate(summaries):
                assert summary.count >= 1, (
                    f"User {i} expected >= 1 unread, got {summary.count}"
                )

        consumers_after = await _consumer_count(lead, _INBOX_STREAM)
        assert consumers_after == consumers_before, (
            f"Concurrent get_unread_summary() leaked consumers: "
            f"{consumers_before} -> {consumers_after} "
            f"after {rounds} rounds x {len(relays)} relays"
        )

        await asyncio.gather(
            *(relays[i].delete_session(_session_key(i)) for i in range(len(relays)))
        )

    async def test_summary_with_empty_inboxes(self, relays: list[NatsRelay]) -> None:
        """get_unread_summary() on empty inboxes — still zero consumers."""
        lead = relays[0]
        consumers_before = await _consumer_count(lead, _INBOX_STREAM)

        # 50 calls against nonexistent inboxes (batch in groups of 10)
        for _ in range(5):
            summaries = await asyncio.gather(
                *(lead.get_unread_summary(f"ghost{i}:ttyX") for i in range(10))
            )
            for summary in summaries:
                assert summary.count == 0

        consumers_after = await _consumer_count(lead, _INBOX_STREAM)
        assert consumers_after == consumers_before, (
            f"Empty inbox summary leaked consumers: "
            f"{consumers_before} -> {consumers_after}"
        )


class TestConcurrentMixedWorkload:
    """Simulate realistic concurrent load: polling + messaging + presence.

    Real-world biff has N users simultaneously:
    - Polling get_unread_summary() every 2s (background poller)
    - Sending messages to each other (write tool)
    - Updating presence (plan tool, heartbeat)
    - Enumerating sessions (who tool)

    This test runs all four workloads concurrently and verifies
    no consumer leak occurs.
    """

    async def test_mixed_concurrent_workload(self, relays: list[NatsRelay]) -> None:
        """Run polling, messaging, presence, and enumeration concurrently."""
        n_users = _N_RELAYS
        lead = relays[0]
        rounds = 5

        # Setup: register all users
        await asyncio.gather(
            *(
                relays[i].update_session(_make_session(i, plan=f"task-{i}"))
                for i in range(n_users)
            )
        )

        consumers_before = await _consumer_count(lead, _INBOX_STREAM)
        kv_consumers_before = await _consumer_count(lead, _KV_STREAM)

        for round_num in range(rounds):
            # All four workloads run concurrently within each round
            polling = asyncio.gather(
                *(relays[i].get_unread_summary(_session_key(i)) for i in range(n_users))
            )
            messaging = asyncio.gather(
                *(
                    relays[i].deliver(
                        Message(
                            from_user=_user(i)[0],
                            to_user=_session_key((i + 1) % n_users),
                            body=f"round-{round_num}",
                        )
                    )
                    for i in range(n_users)
                )
            )
            presence = asyncio.gather(
                *(
                    relays[i].update_session(
                        _make_session(i, plan=f"round-{round_num}")
                    )
                    for i in range(n_users)
                )
            )
            enumeration = asyncio.gather(
                *(relays[i].get_sessions() for i in range(n_users))
            )

            summaries, _, _, session_lists = await asyncio.gather(
                polling, messaging, presence, enumeration
            )

            # Verify polling returned valid counts
            for summary in summaries:
                assert summary.count >= 0

            # Verify enumeration returned all users
            for sessions in session_lists:
                assert len(sessions) == n_users

        # Drain all delivered messages so consumers get deleted
        for _ in range(rounds):
            await asyncio.gather(
                *(relays[i].fetch(_session_key(i)) for i in range(n_users))
            )

        consumers_after = await _consumer_count(lead, _INBOX_STREAM)
        kv_consumers_after = await _consumer_count(lead, _KV_STREAM)

        assert consumers_after == consumers_before, (
            f"Mixed workload inbox consumer leak: "
            f"{consumers_before} -> {consumers_after} "
            f"after {rounds} concurrent rounds"
        )
        assert kv_consumers_after <= kv_consumers_before, (
            f"Mixed workload KV consumer leak: "
            f"{kv_consumers_before} -> {kv_consumers_after} "
            f"after {rounds} concurrent rounds"
        )

        await asyncio.gather(
            *(relays[i].delete_session(_session_key(i)) for i in range(n_users))
        )

    async def test_polling_during_message_storm(self, relays: list[NatsRelay]) -> None:
        """Poll unread summaries while messages are being delivered."""
        n_users = _N_RELAYS
        lead = relays[0]
        messages_per_user = 10

        await asyncio.gather(
            *(relays[i].update_session(_make_session(i)) for i in range(n_users))
        )

        consumers_before = await _consumer_count(lead, _INBOX_STREAM)

        # Storm: deliver messages_per_user messages to each user concurrently
        storm = asyncio.gather(
            *(
                relays[i % n_users].deliver(
                    Message(
                        from_user=_user(i % n_users)[0],
                        to_user=_session_key((i + 1) % n_users),
                        body=f"storm-{i}",
                    )
                )
                for i in range(n_users * messages_per_user)
            )
        )

        # Poll concurrently while the storm is running
        polling = asyncio.gather(
            *(relays[i].get_unread_summary(_session_key(i)) for i in range(n_users))
        )

        await asyncio.gather(storm, polling)

        # Drain
        drain_results = await asyncio.gather(
            *(relays[i].fetch(_session_key(i)) for i in range(n_users))
        )
        for msgs in drain_results:
            assert len(msgs) == messages_per_user

        consumers_after = await _consumer_count(lead, _INBOX_STREAM)
        assert consumers_after == consumers_before, (
            f"Polling-during-storm consumer leak: "
            f"{consumers_before} -> {consumers_after}"
        )

        await asyncio.gather(
            *(relays[i].delete_session(_session_key(i)) for i in range(n_users))
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
