"""InboxFetch: mail a cancelled fetch consumed is filed again, not lost."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from biff.models import Message
from biff.nats_relay import NatsRelay

if TYPE_CHECKING:
    import pytest

_COUNT = 50
_SUBJECT = "biff.test.inbox.eric.tty2"


@dataclass
class _CancelledFetch:
    """A 50-message fetch cancelled half-way through its acks.

    Ack *n* is confirmed after ``0.02 * (n + 1)`` s, as a loaded server would
    answer, and the caller leaves half a second in.  What the server confirmed
    by the time the fetch ended is gone from the queue, so it has to come back.
    """

    relay: NatsRelay
    messages: list[Message]
    confirmed: set[uuid.UUID] = field(default_factory=lambda: set[uuid.UUID]())
    filed: list[Message] = field(default_factory=lambda: list[Message]())

    @classmethod
    async def start(cls, monkeypatch: pytest.MonkeyPatch) -> _CancelledFetch:
        messages = [
            Message(from_user="kai", to_user="eric:tty2", body=f"m{n:02d}")
            for n in range(_COUNT)
        ]
        relay = NatsRelay(url="tls://fake:4222", repo_name="test")
        run = cls(relay, messages)

        def raw(n: int) -> MagicMock:
            async def ack_sync() -> None:
                await asyncio.sleep(0.02 * (n + 1))
                run.confirmed.add(messages[n].id)

            frame = MagicMock()
            frame.data = messages[n].model_dump_json().encode()
            frame.ack_sync = ack_sync
            return frame

        async def deliver(message: Message, **_: object) -> None:
            run.filed.append(message)

        monkeypatch.setattr(relay, "deliver", deliver)
        sub = MagicMock()
        sub.fetch = AsyncMock(return_value=[raw(n) for n in range(_COUNT)])
        sub.unsubscribe = AsyncMock()
        js = MagicMock()
        js.pull_subscribe = AsyncMock(return_value=sub)
        js.delete_consumer = AsyncMock()
        fetch = asyncio.create_task(relay._fetch_from_subject(js, _SUBJECT, "d", "s"))
        await asyncio.sleep(0.5)
        fetch.cancel()
        await asyncio.gather(fetch, return_exceptions=True)
        return run

    def unfiled(self) -> list[Message]:
        """Return the messages the server consumed that were not filed again."""
        refiled = {m.id for m in self.filed}
        return [
            m
            for m in self.messages
            if m.id in self.confirmed and m.refiled().id not in refiled
        ]


async def test_mail_a_cancelled_fetch_consumed_is_filed_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = await _CancelledFetch.start(monkeypatch)
    await run.relay._settle_fetches(5)
    assert len(run.confirmed) == _COUNT  # the fetch ran on to its end
    assert run.unfiled() == []


async def test_closing_the_relay_waits_for_the_mail_a_cancelled_fetch_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A daemon stopping cancels a fetch, then closes the relay: nothing is lost."""
    run = await _CancelledFetch.start(monkeypatch)
    await run.relay.close()
    assert len(run.confirmed) == _COUNT
    assert run.unfiled() == []
