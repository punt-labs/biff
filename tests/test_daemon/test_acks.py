"""``Acknowledgements`` runs a reply's effect on the client's ack, and stays bounded."""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.daemon.acks import _MAX_PENDING, Acknowledgements

if TYPE_CHECKING:
    from biff.daemon.methods import Acknowledge


async def test_confirm_runs_the_acknowledgement_once() -> None:
    ran: list[int] = []

    async def ack() -> None:
        ran.append(1)

    acks = Acknowledgements()
    acks.expect(7, ack)
    await acks.confirm(7)
    await acks.confirm(7)  # already run; an unknown id is a logged no-op
    assert ran == [1]


async def test_forget_drops_a_promise_whose_reply_never_left() -> None:
    ran: list[int] = []

    async def ack() -> None:
        ran.append(1)

    acks = Acknowledgements()
    acks.expect(7, ack)
    acks.forget(7)
    await acks.confirm(7)
    assert ran == []


async def test_expect_past_the_cap_drops_the_oldest_unconfirmed_reply() -> None:
    ran: list[int] = []

    def promise(index: int) -> Acknowledge:
        async def ack() -> None:
            ran.append(index)

        return ack

    acks = Acknowledgements()
    for index in range(_MAX_PENDING + 5):
        acks.expect(index, promise(index))
    assert len(acks) == _MAX_PENDING
    for evicted in range(5):
        await acks.confirm(evicted)  # 0..4 were dropped: nothing runs
    assert ran == []
    await acks.confirm(_MAX_PENDING + 4)  # a retained promise still runs
    assert ran == [_MAX_PENDING + 4]
