"""``CheckWire``: a request that fails midway leaves no subscription behind."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from biff.channel_check_wire import CheckWire

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
        await CheckWire(port).request("biff.channel.check.kai:t", "kai:t")
    assert port.sub.unsubscribed == 1


async def test_a_good_request_keeps_the_reply_inbox_open() -> None:
    port = _Port(fail="")
    pending = await CheckWire(port).request("biff.channel.check.kai:t", "kai:t")
    assert port.sub.unsubscribed == 0
    await pending.close()
    assert port.sub.unsubscribed == 1
