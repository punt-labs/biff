"""A read reply's rows and text are fitted to one byte budget with bounded work."""

from __future__ import annotations

import math

import pytest

from biff.daemon import read_batch as read_batch_module
from biff.daemon.read_batch import ReadBatcher
from biff.formatting import format_read
from biff.models import Message


def _hostile_mail(count: int, chars: int) -> list[Message]:
    """*count* wide-character messages with the longest sender fields."""
    return [
        Message(
            from_user="u" * 120,
            from_tty="t" * 120,
            to_user="kai",
            body="日" * chars,
        )
        for _ in range(count)
    ]


@pytest.mark.parametrize(
    ("budget", "count", "chars"),
    [(4_000, 2, 100), (900, 400, 150), (60_000, 400, 150), (4_000, 1, 500)],
)
def test_fitting_a_reply_renders_a_bounded_number_of_times(
    budget: int, count: int, chars: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Work to fit a batch grows with the log of its size, whatever the mail is."""
    renders: list[int] = []

    def counting(messages: list[Message]) -> str:
        renders.append(len(messages))
        return format_read(messages)

    monkeypatch.setattr(read_batch_module, "format_read", counting)
    pending = _hostile_mail(count, chars)
    fitted = ReadBatcher(budget).fit(pending)
    assert fitted.size <= budget or len(fitted.messages) == 1
    assert len(renders) <= math.ceil(math.log2(max(len(pending), 2))) + 4


def test_an_empty_inbox_fits_as_no_new_messages() -> None:
    fitted = ReadBatcher(4_000).fit([])
    assert (fitted.messages, fitted.text) == ([], "No new messages.")
