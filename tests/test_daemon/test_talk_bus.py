"""The talk bus counts subscribes per subject, never as one total."""

from __future__ import annotations

from tests.test_daemon.talk_bus import BusMessage, TalkBus


async def _ignore(msg: BusMessage) -> None:
    del msg


async def test_a_neighbours_resubscribe_does_not_count_for_this_subject() -> None:
    """Kai's feed can resubscribe before eric's: eric's old one still holds."""
    bus = TalkBus()
    await bus.subscribe("talk.notify.eric", _ignore)
    await bus.flush()
    before = bus.subscribes_to("talk.notify.eric")
    await bus.subscribe("talk.notify.kai", _ignore)  # the neighbour moves first
    assert bus.subscribes_to("talk.notify.eric") == before


async def test_a_subscribe_counts_for_its_own_subject() -> None:
    bus = TalkBus()
    await bus.subscribe("talk.notify.eric", _ignore)
    assert bus.subscribes_to("talk.notify.eric") == 1
