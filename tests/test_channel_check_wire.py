"""``CheckWire``: a request that fails midway leaves no subscription behind."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

import pytest

from biff.channel_check_pending import (
    CheckLostError,
    CheckRefusal,
    CheckRefusedError,
    PendingCheck,
)
from biff.channel_check_wire import CheckAck

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from nats.aio.msg import Msg
    from nats.aio.subscription import Subscription


class _Sub:
    def __init__(self) -> None:
        self.unsubscribed = 0

    async def unsubscribe(self) -> None:
        self.unsubscribed += 1


class _Port:
    """A client whose flush or publish can fail."""

    def __init__(self, *, fail: str) -> None:
        self.sub = _Sub()
        self.fail = fail

    def new_inbox(self) -> str:
        return "_INBOX.x"

    async def subscribe(
        self,
        subject: str,
        queue: str = "",
        cb: Callable[[Msg], Awaitable[None]] | None = None,
    ) -> Subscription:
        return cast("Subscription", self.sub)

    async def flush(self) -> None:
        if self.fail == "flush":
            raise ConnectionError("flush failed")

    async def publish(self, subject: str, payload: bytes) -> None:
        if self.fail == "publish":
            raise ConnectionError("publish failed")


@pytest.mark.parametrize("fail", ["flush", "publish"])
async def test_a_failed_request_unsubscribes_the_reply_inbox(fail: str) -> None:
    port = _Port(fail=fail)
    with pytest.raises(ConnectionError, match=fail):
        await PendingCheck.start(port, "biff.channel.check.kai:t", "kai:t")
    assert port.sub.unsubscribed == 1


async def test_a_good_request_keeps_the_reply_inbox_open() -> None:
    port = _Port(fail="")
    pending = await PendingCheck.start(port, "biff.channel.check.kai:t", "kai:t")
    assert port.sub.unsubscribed == 0
    await pending.close()
    assert port.sub.unsubscribed == 1


class _Msg:
    def __init__(self, data: bytes) -> None:
        self.data = data


class _Messages:
    """A reply subscription that yields *frames*, then ends as a closed one does."""

    def __init__(self, frames: list[bytes], *, then_wait: bool = False) -> None:
        self._frames = list(frames)
        self._then_wait = then_wait

    def __aiter__(self) -> _Messages:
        return self

    async def __anext__(self) -> _Msg:
        if self._frames:
            return _Msg(self._frames.pop(0))
        if self._then_wait:
            await asyncio.Event().wait()
        raise StopAsyncIteration


class _ReplySub:
    def __init__(self, messages: _Messages) -> None:
        self.messages = messages


def _pending(*frames: bytes, then_wait: bool = False) -> PendingCheck:
    sub = _ReplySub(_Messages(list(frames), then_wait=then_wait))
    return PendingCheck(cast("Subscription", sub), "kai:t")


async def test_a_closed_reply_subscription_is_a_lost_relay_not_silence() -> None:
    with pytest.raises(CheckLostError, match="relay connection closed"):
        await _pending().confirmed(1)


async def test_silence_is_a_timeout_not_a_lost_relay() -> None:
    assert await _pending(then_wait=True).confirmed(0.05) is False


async def test_a_refusal_is_raised_with_its_reason() -> None:
    refusal = CheckRefusal("rate-limited (60 s)").to_payload()
    with pytest.raises(CheckRefusedError, match="rate-limited"):
        await _pending(refusal).confirmed(1)


async def test_an_unreadable_reply_is_logged_and_the_wait_goes_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ack = CheckAck("kai:t", "tok").to_payload()
    with caplog.at_level(logging.WARNING):
        assert await _pending(b"not json", ack).confirmed(1) is True
    assert "unreadable channel check reply" in caplog.text
