"""Mail is marked read only after the reply that carries it has been sent.

Every test runs twice: over a relay whose fetch leaves mail unread until it is
marked (the local relay), and over one whose fetch consumes it (what the NATS
relay does: its fetch acks, a work queue deletes on ack, and ``mark_read`` does
nothing).  Mail the daemon fetched but could not deliver must survive both.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from biff.client.connection import DaemonError
from biff.commands.inbox import Inbox
from biff.daemon import connection as connection_module
from biff.daemon.connection import ConnectionHandler
from biff.daemon.methods import Methods
from biff.daemon.outbox import Outbox
from biff.daemon.session_drain import InboxDrain
from biff.frame_reply import ErrorCode
from biff.models import Message, UnreadSummary
from biff.relay import LocalRelay
from biff.session_id import CLI_ROLE
from biff.session_routing import SessionRouting

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence
    from pathlib import Path

    from biff.cli_context import CliContext
    from biff.daemon.sessions import Session
    from tests.test_daemon.conftest import Harness


class ConsumingRelay(LocalRelay):
    """A relay faithful to ``NatsRelay`` fetch semantics: fetching consumes.

    ``NatsRelay.fetch`` acks every frame it pulls, a work queue deletes an acked
    frame, and ``mark_read`` is a no-op because the mail is already gone.
    """

    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)

    consumes_on_fetch = True

    async def get_unread_summary(self, session_key: str) -> UnreadSummary:
        """Count without consuming: the NATS relay counts from stream info."""
        tty = await LocalRelay.fetch(self, session_key)
        user = await LocalRelay.fetch_user_inbox(self, session_key.split(":")[0])
        return UnreadSummary(count=len(tty) + len(user))

    async def fetch(self, session_key: str) -> list[Message]:
        unread = await super().fetch(session_key)
        await super().mark_read(session_key, [m.id for m in unread])
        return unread

    async def fetch_user_inbox(self, user: str) -> list[Message]:
        unread = await super().fetch_user_inbox(user)
        await super().mark_read_user_inbox(user, [m.id for m in unread])
        return unread

    async def mark_read(self, session_key: str, ids: Sequence[uuid.UUID]) -> None:
        return None

    async def mark_read_user_inbox(self, user: str, ids: Sequence[uuid.UUID]) -> None:
        return None


class RefusingRelay(ConsumingRelay):
    """A consuming relay that can be told to refuse every delivery."""

    refusing = False

    async def deliver(
        self,
        message: Message,
        *,
        sender_key: str = "",
        target_repo: str | None = None,
    ) -> None:
        if RefusingRelay.refusing:
            msg = "relay refused the delivery"
            raise ConnectionError(msg)
        await super().deliver(message, sender_key=sender_key, target_repo=target_repo)


@pytest.fixture(autouse=True, params=["local", "consuming"])
def relay_kind(request: pytest.FixtureRequest, harness: Harness) -> str:
    kind = cast("str", request.param)
    harness.factory.relay_type = LocalRelay if kind == "local" else ConsumingRelay
    return kind


def _rows(result: object) -> list[dict[str, str]]:
    return cast("list[dict[str, str]]", cast("dict[str, object]", result)["data"])


def _bodies(result: object) -> list[str]:
    return [row["body"] for row in _rows(result)]


def _more(result: object) -> int:
    return cast("int", cast("dict[str, object]", result).get("more", 0))


def _json_bytes(row: dict[str, str]) -> int:
    return len(json.dumps(row, ensure_ascii=False).encode())


async def _lose_replies(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, client_reads: int
) -> None:
    """Make *client_reads* reads whose replies never leave the daemon."""
    kai = harness.clients[0]

    async def failing_send(_ws: object, _frame: object) -> bool:
        return False  # the connection went away before the reply left

    with monkeypatch.context() as patch:
        patch.setattr(ConnectionHandler, "_send", staticmethod(failing_send))
        for _ in range(client_reads):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(kai.request("read"), 0.3)


async def test_a_reply_that_cannot_be_sent_does_not_consume_the_mail(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await eric.request("write", to="kai", message="do not lose me")
    await _lose_replies(harness, monkeypatch, 1)
    assert "do not lose me" in _bodies(await kai.request("read"))


async def test_undelivered_mail_stays_unread_across_two_reads(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    for n in range(3):
        await eric.request("write", to="kai", message=f"letter {n}")
    await _lose_replies(harness, monkeypatch, 2)
    assert _bodies(await kai.request("read")) == ["letter 0", "letter 1", "letter 2"]
    assert _bodies(await kai.request("read")) == []


async def test_a_held_reply_marks_mail_read_only_when_confirmed(
    harness: Harness,
) -> None:
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await eric.request("write", to="kai", message="not yet")
    held = await kai.request_held("read")
    assert _bodies(held.result) == ["not yet"]
    again = await kai.request_held("read")  # nothing confirmed: still unread
    assert _bodies(again.result) == ["not yet"]
    await again.confirm()
    assert _bodies(await kai.request("read")) == []


async def test_a_sent_reply_consumes_the_mail_it_carried(harness: Harness) -> None:
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await eric.request("write", to="kai", message="once only")
    assert _bodies(await kai.request("read")) == ["once only"]
    assert _bodies(await kai.request("read")) == []


async def test_an_oversized_inbox_is_delivered_in_batches_with_none_lost(
    harness: Harness,
) -> None:
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    sent = [f"message {n:03d} " + "x" * 200 for n in range(30)]
    for body in sent:
        await eric.request("write", to="kai", message=body)
    received: list[str] = []
    for _ in range(30):
        result = await kai.request("read")
        received += _bodies(result)
        if not _more(result):
            break
    assert received == sent


async def test_a_batch_that_leaves_mail_behind_says_how_much(harness: Harness) -> None:
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    for n in range(30):
        await eric.request("write", to="kai", message=f"m{n:03d} " + "x" * 200)
    first = await kai.request("read")
    assert 0 < len(_bodies(first)) < 30
    assert _more(first) == 30 - len(_bodies(first))


async def test_a_batch_is_never_empty_when_mail_waits(harness: Harness) -> None:
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await eric.request("write", to="kai", message="y" * 400)
    assert len(_bodies(await kai.request("read"))) == 1


async def test_the_budget_counts_bytes_not_characters(harness: Harness) -> None:
    """Three-byte characters: a character count would let a batch overshoot."""
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    budget = harness.daemon._core.methods._read_budget
    for n in range(12):
        await eric.request("write", to="kai", message=f"{n:02d}" + "日" * 400)
    batches: list[list[dict[str, str]]] = []
    for _ in range(12):
        result = await kai.request("read")
        batches.append(_rows(result))
        if not _more(result):
            break
    assert sum(len(batch) for batch in batches) == 12
    for batch in batches:
        assert sum(_json_bytes(row) for row in batch) <= budget


async def test_a_message_larger_than_the_budget_is_clipped_not_sent_whole(
    harness: Harness,
) -> None:
    kai = await harness.connect("s-kai", "kai")
    harness.daemon._core.methods._read_budget = 700
    sender = harness.factory.relay_type(harness.factory.built[0])
    await sender.deliver(
        Message(from_user="eric", to_user="kai", body="z" * 512), sender_key="eric:t"
    )
    first = await kai.request("read")
    (row,) = _rows(first)
    assert row["body"].endswith("[clipped]")
    assert _json_bytes(row) <= 700
    assert (_more(first), _bodies(await kai.request("read"))) == (0, [])


async def test_a_reply_over_the_frame_limit_is_a_named_error_and_loses_nothing(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    for n in range(6):
        await eric.request("write", to="kai", message=f"big {n} " + "q" * 300)
    harness.daemon._core.methods._read_budget = 10_000_000
    with monkeypatch.context() as patch:
        patch.setattr(connection_module, "MAX_FRAME_BYTES", 1500)
        with pytest.raises(DaemonError, match=r"frame limit") as caught:
            await kai.request("read")
    assert caught.value.code is ErrorCode.INTERNAL
    harness.daemon._core.methods._read_budget = 4000
    received: list[str] = []
    for _ in range(10):
        result = await kai.request("read")
        received += _bodies(result)
        if not _more(result):
            break
    assert len(received) == 6


async def test_a_batch_filled_to_the_budget_fits_one_frame_with_its_text(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rendered text rides in the same frame as the rows, so it is budgeted.

    The budget is half the frame limit, as in production.  Rows alone filling
    the budget would leave the table text, about as large again, to overflow.
    """
    frame_limit = 20_000
    monkeypatch.setattr(connection_module, "MAX_FRAME_BYTES", frame_limit)
    harness.daemon._core.methods._read_budget = frame_limit // 2
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    # Wide characters: the table is wider in bytes than the JSON rows it renders.
    sent = [f"{n:03d}" + "日" * 300 for n in range(40)]
    for body in sent:
        await eric.request("write", to="kai", message=body)
    received: list[str] = []
    for _ in range(40):
        result = await kai.request("read")  # a frame over the limit raises here
        received += _bodies(result)
        if not _more(result):
            break
    assert received == sent


async def test_mail_a_batch_left_behind_moves_with_the_session_when_it_ends(
    harness: Harness,
) -> None:
    """``session end`` drains the relay; mail the daemon held must be there."""
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    for n in range(30):
        await eric.request("write", to="kai", message=f"m{n:03d} " + "x" * 200)
    first = await kai.request("read")
    left_behind = 30 - len(_bodies(first))
    assert left_behind > 0
    await asyncio.sleep(0.2)  # the daemon marks the delivered batch read
    relay = harness.factory.relay_type(harness.factory.built[0])
    key = SessionRouting("s-kai", role=CLI_ROLE).session_key("kai")
    await InboxDrain(relay).move(key, "kai")
    reachable = [*await relay.fetch(key), *await relay.fetch_user_inbox("kai")]
    assert len(reachable) == left_behind


async def test_filing_a_remainder_again_leaves_one_copy(harness: Harness) -> None:
    """Refiling the same message twice must not make two copies of it."""
    await harness.connect("s-kai", "kai")
    relay = harness.factory.relay_type(harness.factory.built[0])
    key = SessionRouting("s-kai", role=CLI_ROLE).session_key("kai")
    message = Message(from_user="eric", to_user=key, body="remainder")
    await relay.deliver(message.refiled())
    await relay.deliver(message.refiled())
    fetched = await relay.fetch(key)
    assert [m.body for m in fetched] == ["remainder"]


async def test_a_remainder_the_relay_refuses_is_offered_again_by_the_next_read(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused refile keeps the mail in the outbox: none lost, none twice."""
    harness.factory.relay_type = RefusingRelay
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    sent = [f"message {n:03d} " + "x" * 200 for n in range(30)]
    for body in sent:
        await eric.request("write", to="kai", message=body)
    with monkeypatch.context() as patch:
        patch.setattr(RefusingRelay, "refusing", True)
        first = await kai.request("read")
    received = _bodies(first)
    assert _more(first) == len(sent) - len(received)
    for _ in range(30):
        result = await kai.request("read")
        received += _bodies(result)
        if not _more(result):
            break
    assert received == sent


class CancellingRelay(ConsumingRelay):
    """A consuming relay whose connection ends after a few deliveries."""

    allowed: int | None = None

    async def deliver(
        self,
        message: Message,
        *,
        sender_key: str = "",
        target_repo: str | None = None,
    ) -> None:
        if CancellingRelay.allowed is not None:
            if CancellingRelay.allowed == 0:
                raise asyncio.CancelledError
            CancellingRelay.allowed -= 1
        await super().deliver(message, sender_key=sender_key, target_repo=target_repo)


async def test_a_read_cancelled_between_refiles_leaves_one_copy_of_each_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each refiled message leaves the outbox as it is filed, not after the loop."""
    relay = CancellingRelay(tmp_path)
    key = SessionRouting("s-kai", role=CLI_ROLE).session_key("kai")
    mail = [Message(from_user="eric", to_user=key, body=f"m{n}") for n in range(5)]
    outbox = Outbox(retains=True)
    pending = outbox.merge(mail)
    ctx = cast("CliContext", SimpleNamespace(relay=relay))
    session = cast("Session", SimpleNamespace(outbox=outbox))
    with monkeypatch.context() as patch:
        patch.setattr(CancellingRelay, "allowed", 2)
        with pytest.raises(asyncio.CancelledError):
            await Methods._refile(session, Inbox(ctx, [], []), pending, pending[:1])
    filed = [m.body for m in await relay.fetch(key)]
    held = [m.body for m in outbox.merge([])]
    assert sorted([*filed, *held]) == sorted(m.body for m in mail)


class LandingTimeoutRelay(ConsumingRelay):
    """A relay whose write lands but is reported as timed out."""

    timing_out = False

    async def deliver(
        self,
        message: Message,
        *,
        sender_key: str = "",
        target_repo: str | None = None,
    ) -> None:
        await super().deliver(message, sender_key=sender_key, target_repo=target_repo)
        if LandingTimeoutRelay.timing_out:
            raise TimeoutError


async def test_a_refile_that_landed_but_timed_out_is_read_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The filed copy and the held original are one message on the next read."""
    relay = LandingTimeoutRelay(tmp_path)
    key = SessionRouting("s-kai", role=CLI_ROLE).session_key("kai")
    mail = [Message(from_user="eric", to_user=key, body=f"m{n}") for n in range(4)]
    outbox = Outbox(retains=True)
    pending = outbox.merge(mail)
    ctx = cast("CliContext", SimpleNamespace(relay=relay))
    session = cast("Session", SimpleNamespace(outbox=outbox))
    with monkeypatch.context() as patch:
        patch.setattr(LandingTimeoutRelay, "timing_out", True)
        filed = await Methods._refile(session, Inbox(ctx, [], []), pending, pending[:1])
    assert filed == 0  # every write was reported as failed, though each landed
    merged = outbox.merge(await relay.fetch(key))
    assert sorted(m.body for m in merged) == ["m0", "m1", "m2", "m3"]


async def test_more_after_a_refile_that_landed_but_timed_out_stays_within_its_bound(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`more` before reconciliation: never below unread, above by landed copies."""
    harness.factory.relay_type = LandingTimeoutRelay
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    sent = [f"message {n:03d} " + "x" * 200 for n in range(30)]
    for body in sent:
        await eric.request("write", to="kai", message=body)
    with monkeypatch.context() as patch:
        patch.setattr(LandingTimeoutRelay, "timing_out", True)
        first = await kai.request("read")
    unread = len(sent) - len(_bodies(first))
    landed = unread  # every refile of the remainder landed and was reported failed
    assert unread <= _more(first) <= unread + landed
