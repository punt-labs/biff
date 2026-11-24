"""``NatsRelay.inbox_high_water``: mail ordering by stream sequence (DES-087)."""

from __future__ import annotations

import pytest

from biff.models import Message
from biff.nats_relay import NatsRelay

pytestmark = pytest.mark.nats_local

_KEY = "eric:tty2"


def _to_session(body: str = "hi") -> Message:
    return Message(from_user="kai", to_user=_KEY, body=body)


def _to_user(body: str = "hi") -> Message:
    return Message(from_user="kai", to_user="eric", body=body)


class TestInboxHighWater:
    async def test_empty_inbox_is_zero(self, relay: NatsRelay) -> None:
        assert await relay.inbox_high_water(_KEY) == 0

    async def test_rises_with_each_session_message(self, relay: NatsRelay) -> None:
        await relay.deliver(_to_session())
        first = await relay.inbox_high_water(_KEY)
        await relay.deliver(_to_session())
        assert await relay.inbox_high_water(_KEY) > first > 0

    async def test_rises_with_a_broadcast_message(self, relay: NatsRelay) -> None:
        await relay.deliver(_to_session())
        before = await relay.inbox_high_water(_KEY)
        await relay.deliver(_to_user())
        assert await relay.inbox_high_water(_KEY) > before

    async def test_rises_when_the_count_is_flat(self, relay: NatsRelay) -> None:
        """Two read, two arrive: the unread count is the same, the mark is not."""
        await relay.deliver(_to_session("a"))
        await relay.deliver(_to_session("b"))
        count = (await relay.get_unread_summary(_KEY)).count
        before = await relay.inbox_high_water(_KEY)
        await relay.fetch(_KEY)  # a read deletes the messages
        await relay.deliver(_to_session("c"))
        await relay.deliver(_to_session("d"))
        assert (await relay.get_unread_summary(_KEY)).count == count
        assert await relay.inbox_high_water(_KEY) > before

    async def test_other_inboxes_do_not_raise_it(self, relay: NatsRelay) -> None:
        await relay.deliver(_to_session())
        before = await relay.inbox_high_water(_KEY)
        await relay.deliver(Message(from_user="kai", to_user="eric:tty9", body="x"))
        await relay.deliver(Message(from_user="kai", to_user="sam", body="x"))
        assert await relay.inbox_high_water(_KEY) == before

    async def test_a_read_may_lower_it_but_the_next_arrival_exceeds_any_mark(
        self, relay: NatsRelay
    ) -> None:
        await relay.deliver(_to_session())
        mark = await relay.inbox_high_water(_KEY)
        await relay.fetch(_KEY)
        assert await relay.inbox_high_water(_KEY) <= mark
        await relay.deliver(_to_session())
        assert await relay.inbox_high_water(_KEY) > mark
