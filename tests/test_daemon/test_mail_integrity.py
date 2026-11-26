"""Mail the daemon serves is neither delivered twice, lost, nor miscounted.

Two relay behaviours matter: a relay whose fetch leaves mail unread until it is
marked read (the local relay), and one whose fetch consumes it, here capped at
the size of one pull as the NATS relay is.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import pytest

from biff.client.connection import ClientIdentity, DaemonClient, RequestTimeoutError
from biff.daemon.connection import ConnectionHandler
from biff.relay import LocalRelay
from tests.test_daemon.test_read_delivery import ConsumingRelay

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from biff.models import Message, UnreadSummary
    from tests.test_daemon.conftest import Harness

_PULL_CAP = 100
_WAIT = 3.0


class CappedRelay(ConsumingRelay):
    """A consuming relay whose one fetch returns at most ``_PULL_CAP`` messages."""

    async def fetch_user_inbox(self, user: str) -> list[Message]:
        unread = (await LocalRelay.fetch_user_inbox(self, user))[:_PULL_CAP]
        await LocalRelay.mark_read_user_inbox(self, user, [m.id for m in unread])
        return unread


class SlowLocalRelay(LocalRelay):
    """A local relay whose broadcast fetch can be made slower than a client waits."""

    delay = 0.0

    async def fetch_user_inbox(self, user: str) -> list[Message]:
        unread = await super().fetch_user_inbox(user)
        await asyncio.sleep(type(self).delay)
        return unread


class SlowConsumingRelay(ConsumingRelay):
    """A consuming relay whose broadcast fetch can outlast a client's patience."""

    delay = 0.0

    async def fetch_user_inbox(self, user: str) -> list[Message]:
        unread = await super().fetch_user_inbox(user)
        await asyncio.sleep(type(self).delay)
        return unread


def _bodies(result: object) -> list[str]:
    rows = cast("list[dict[str, str]]", cast("dict[str, object]", result)["data"])
    return [row["body"] for row in rows]


def _more(result: object) -> int:
    return cast("int", cast("dict[str, object]", result).get("more", 0))


async def test_two_sessions_sharing_a_broadcast_inbox_never_get_one_message_twice(
    harness: Harness,
) -> None:
    first = await harness.connect("s-one", "kai")
    second = await harness.connect("s-two", "kai")
    eric = await harness.connect("s-eric", "eric")
    sent = [f"m{n:03d} " + "x" * 200 for n in range(20)]
    for body in sent:
        await eric.request("write", to="kai", message=body)
    received: list[str] = []
    for _ in range(20):
        for session in (first, second):
            received += _bodies(await session.request("read"))
    assert sorted(received) == sorted(sent)


async def test_a_consuming_relay_reports_mail_beyond_one_pull_as_more(
    harness: Harness,
) -> None:
    harness.factory.relay_type = CappedRelay
    harness.daemon._core.methods._read_budget = 200_000
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    total = _PULL_CAP + 30
    for n in range(total):
        await eric.request("write", to="kai", message=f"m{n:03d}")
    first = await kai.request("read")
    assert (len(_bodies(first)), _more(first)) == (_PULL_CAP, 30)
    received = _bodies(first)
    for _ in range(5):
        if not _more(first):
            break
        first = await kai.request("read")
        received += _bodies(first)
    assert (len(received), len(set(received)), _more(first)) == (total, total, 0)


async def test_mail_held_in_the_outbox_is_in_a_reconnecting_clients_unread_count(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.factory.relay_type = ConsumingRelay
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    for n in range(3):
        await eric.request("write", to="kai", message=f"held {n}")

    async def failing_send(_ws: object, _frame: object) -> bool:
        return False  # the connection went away before the reply left

    with monkeypatch.context() as patch:
        patch.setattr(ConnectionHandler, "_send", staticmethod(failing_send))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(kai.request("read"), 0.3)
    reconnected = await harness.connect("s-kai", "kai")
    await reconnected.subscribe()
    event = await asyncio.wait_for(reconnected.next_event(), _WAIT)
    assert (event.kind, event.data) == ("unread", {"count": 3})


async def _short_patience_client(harness: Harness, timeout: float) -> DaemonClient:
    client = await DaemonClient.connect(
        run_dir=harness.run_dir,
        port=harness.daemon.port,
        identity=ClientIdentity(
            client="cli", session_id="s-kai", cwd="/work/repo", user="kai"
        ),
        request_timeout=timeout,
    )
    harness.clients.append(client)
    return client


@pytest.mark.parametrize("relay_type", [SlowLocalRelay, SlowConsumingRelay])
async def test_a_reply_the_client_gave_up_on_does_not_consume_the_mail(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    relay_type: Callable[[Path], LocalRelay],
) -> None:
    harness.factory.relay_type = relay_type
    kai = await _short_patience_client(harness, 0.1)
    eric = await harness.connect("s-eric", "eric")
    await eric.request("write", to="kai", message="do not lose me")
    monkeypatch.setattr(relay_type, "delay", 0.25)
    with pytest.raises(RequestTimeoutError):
        await kai.request("read")  # the daemon replies after the client left
    await asyncio.sleep(0.4)  # the late reply was written and discarded
    monkeypatch.setattr(relay_type, "delay", 0.0)
    assert _bodies(await kai.request("read")) == ["do not lose me"]


class SummaryFailsRelay(ConsumingRelay):
    """A consuming relay whose backlog summary fails after a good fetch."""

    async def get_unread_summary(self, session_key: str) -> UnreadSummary:
        msg = "backlog summary unavailable"
        raise OSError(msg)


async def test_a_backlog_summary_failure_still_delivers_the_fetched_batch(
    harness: Harness,
) -> None:
    harness.factory.relay_type = SummaryFailsRelay
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await eric.request("write", to="kai", message="deliver me")
    assert _bodies(await kai.request("read")) == ["deliver me"]
