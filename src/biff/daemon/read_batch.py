"""Fit a batch of mail, with its rendered text, into one reply's byte budget."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, NamedTuple, Self, final

from pydantic import JsonValue, TypeAdapter
from pydantic_core import to_jsonable_python

from biff.formatting import format_read

if TYPE_CHECKING:
    from biff.models import Message

__all__ = ["FLOOR_BYTES", "ReadBatch", "ReadBatcher"]

_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)

FLOOR_BYTES = 8_192
"""The size no single clipped message exceeds, with every field at its bound.

The worst case measures 8,160 bytes (every name field full, each character one
that JSON writes in six bytes).  A budget below this may be exceeded by a lone
message; any budget the daemon runs with is far above it.
"""

_ENVELOPE_BYTES = 512
"""What a reply adds around its messages: ids, keys, the ``more`` count."""

_CLIPPED = "[clipped]"


class ReadBatch(NamedTuple):
    """A batch as the reply carries it, and the reply's size in bytes."""

    messages: list[Message]
    data: JsonValue
    text: str
    size: int


@final
class ReadBatcher:
    """Chooses how much mail one ``read`` reply carries.

    The reply holds the rows and the table text rendered from them, in one
    frame, so the budget covers both.  For a budget of at least
    :data:`FLOOR_BYTES` a returned reply measures at most the budget, which is
    at most half the frame limit, so the frame's own fields (id, type, version,
    ``more``) always fit in the other half.  Below the floor a lone message may
    exceed the budget, since it cannot be made smaller than one clipped message.

    The work is bounded whatever the mail is: rows alone pre-select the
    candidates in one linear pass with no rendering, then a binary search
    renders ``ceil(log2(n + 1))`` times to find the longest prefix that fits,
    and a first message that cannot fit alone is clipped in one measured step
    and rendered at most four more times.
    """

    __slots__ = ("_budget",)

    _budget: int

    def __new__(cls, budget: int) -> Self:
        """Create a batcher whose replies measure at most *budget* bytes."""
        self = super().__new__(cls)
        self._budget = budget
        return self

    def fit(self, pending: list[Message]) -> ReadBatch:
        """Return the longest prefix of *pending* whose reply fits the budget.

        A first message too large to fit alone is clipped, never sent whole.
        """
        candidates = self._by_rows(pending)
        low, high = 0, len(candidates)  # prefix lengths: low fits, high is the cap
        best = self._render([])
        while low < high:
            mid = (low + high + 1) // 2
            attempt = self._render(candidates[:mid])
            if attempt.size <= self._budget:
                low, best = mid, attempt
            else:
                high = mid - 1
        if candidates and low == 0:
            best = self._render([self._clipped_to_fit(candidates[0])])
        return best

    @staticmethod
    def _render(messages: list[Message]) -> ReadBatch:
        """Return *messages* as wire rows and text, with the size of the reply."""
        rows = [m.model_dump(mode="json") for m in messages]
        data = _JSON.validate_python(to_jsonable_python(rows))
        text = format_read(messages) if messages else "No new messages."
        reply = {"data": data, "error": False, "text": text}
        size = len(json.dumps(reply, ensure_ascii=False).encode())
        return ReadBatch(messages, data, text, size)

    def _by_rows(self, pending: list[Message]) -> list[Message]:
        """Take messages from the front while their JSON rows fit the budget."""
        batch: list[Message] = []
        used = _ENVELOPE_BYTES
        for message in pending:
            size = len(message.model_dump_json().encode()) + 1  # the comma
            if not batch and used + size > self._budget:
                bare = len(
                    message.model_copy(update={"body": "x"}).model_dump_json().encode()
                )
                room = self._budget - used - bare - len(_CLIPPED.encode())
                message = self._clip(message, room)
                size = len(message.model_dump_json().encode()) + 1
            elif used + size > self._budget:
                break
            batch.append(message)
            used += size
        return batch

    def _clipped_to_fit(self, message: Message) -> Message:
        """Clip *message*'s body so its reply fits the budget, by a measured amount.

        The reply grows about linearly with the body, so one measurement of the
        whole message and one of its bare form give the slope; a clip by that
        slope is checked once and, if the estimate was short, falls back to the
        smallest clipped message.  A constant number of renders, never a search.
        """
        bare = self._clip(message, 0)
        floor = self._render([bare]).size
        whole = self._render([message]).size
        body_bytes = len(message.body.encode())
        grown = max(1, whole - floor)
        room = max(0, self._budget - floor) * body_bytes // grown
        guess = self._clip(message, room)
        if self._render([guess]).size <= self._budget:
            return guess
        return bare

    @staticmethod
    def _clip(message: Message, keep: int) -> Message:
        """Cut *message*'s body to *keep* bytes, marked as clipped."""
        body = message.body.encode()[: max(1, keep)].decode(errors="ignore") or "x"
        return message.model_copy(update={"body": body + _CLIPPED})
