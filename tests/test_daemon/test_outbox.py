"""The outbox holds fetched mail once, however often it was filed again."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from biff.daemon.outbox import Outbox
from biff.models import Message

_NOON = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)


def _mail(body: str, *, at: datetime = _NOON, n: int | None = None) -> Message:
    fields: dict[str, object] = {
        "from_user": "eric",
        "to_user": "kai:tty1",
        "body": body,
        "timestamp": at,
    }
    if n is not None:
        fields["id"] = uuid.UUID(int=n)
    return Message.model_validate(fields)


def test_a_filed_copy_of_a_held_message_is_the_same_message() -> None:
    outbox = Outbox(retains=True)
    original = _mail("once")
    outbox.merge([original])
    merged = outbox.merge([original.refiled()])
    assert [m.body for m in merged] == ["once"]
    assert len(outbox) == 1


def test_a_copy_filed_twice_over_is_still_one_message() -> None:
    outbox = Outbox(retains=True)
    original = _mail("once")
    outbox.merge([original])
    outbox.merge([original.refiled()])
    merged = outbox.merge([original.refiled().refiled()])
    assert [m.body for m in merged] == ["once"]


def test_a_batch_is_oldest_first_and_ties_break_by_id() -> None:
    later = _mail("later", at=_NOON + timedelta(seconds=1), n=1)
    tie_high = _mail("tie high", n=9)
    tie_low = _mail("tie low", n=2)
    for retains in (True, False):
        merged = Outbox(retains=retains).merge([later, tie_high, tie_low])
        assert [m.body for m in merged] == ["tie low", "tie high", "later"]
