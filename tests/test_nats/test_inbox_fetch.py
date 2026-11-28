"""InboxFetch: mail a cancelled fetch consumed is filed again, not lost."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from biff.models import Message
from biff.nats_relay import NatsRelay

_COUNT = 50
_SUBJECT = "biff.test.inbox.eric.tty2"


async def test_mail_a_cancelled_fetch_consumed_is_filed_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller cancelled mid-fetch (a deadline) must not lose the mail consumed.

    Ack *n* is confirmed after ``0.02 * (n + 1)`` s, as a loaded server would
    answer; the caller leaves half a second in.  Whatever the server confirmed
    by the time the fetch ends is gone from the queue, so it has to come back.
    """
    messages = [
        Message(from_user="kai", to_user="eric:tty2", body=f"m{n:02d}")
        for n in range(_COUNT)
    ]
    confirmed: set[uuid.UUID] = set()

    def raw(n: int) -> MagicMock:
        async def ack_sync() -> None:
            await asyncio.sleep(0.02 * (n + 1))
            confirmed.add(messages[n].id)

        frame = MagicMock()
        frame.data = messages[n].model_dump_json().encode()
        frame.ack_sync = ack_sync
        return frame

    relay = NatsRelay(url="tls://fake:4222", repo_name="test")
    filed: list[Message] = []
    all_filed = asyncio.Event()

    async def deliver(message: Message, **_: object) -> None:
        filed.append(message)
        if len(filed) == _COUNT:
            all_filed.set()

    monkeypatch.setattr(relay, "deliver", deliver)
    sub = MagicMock()
    sub.fetch = AsyncMock(return_value=[raw(n) for n in range(_COUNT)])
    sub.unsubscribe = AsyncMock()
    js = MagicMock()
    js.pull_subscribe = AsyncMock(return_value=sub)
    js.delete_consumer = AsyncMock()

    fetch = asyncio.create_task(relay._fetch_from_subject(js, _SUBJECT, "d"))
    await asyncio.sleep(0.5)
    fetch.cancel()
    await asyncio.gather(fetch, return_exceptions=True)
    await asyncio.wait_for(all_filed.wait(), timeout=5)

    refiled = {m.id for m in filed}
    assert all(m.refiled().id in refiled for m in messages if m.id in confirmed)
    assert len(confirmed) == _COUNT  # the fetch ran on to its end
