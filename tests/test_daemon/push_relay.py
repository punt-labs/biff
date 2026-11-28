"""A relay that pushes arrival notices over an in-memory bus, as the NATS relay does."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, final

from biff.channel_check_pending import (
    CheckLostError,
    CheckRefusal,
    CheckRefusedError,
)
from biff.channel_check_wire import CheckAck, CheckRequest
from tests.test_daemon.talk_bus import TalkBusRelay

if TYPE_CHECKING:
    from pathlib import Path

    from biff.models import Message, WallPost
    from tests.test_daemon.talk_bus import BusMessage, TalkBus

logger = logging.getLogger(__name__)


@final
class FakePending:
    """The doctor's wait for one session's acknowledgement."""

    def __init__(self, session_key: str) -> None:
        self._session_key = session_key
        self._replies: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def receive(self, msg: BusMessage) -> None:
        self._replies.put_nowait(msg.data)

    def lose(self) -> None:
        """End the wait as a dropped relay connection does."""
        self._replies.put_nowait(None)

    async def confirmed(self, timeout: float) -> bool:
        try:
            async with asyncio.timeout(timeout):
                while (data := await self._replies.get()) is not None:
                    if reason := CheckRefusal.of(data):
                        raise CheckRefusedError(reason)
                    if CheckAck.from_payload(data).session_key == self._session_key:
                        return True
        except TimeoutError:
            return False
        raise CheckLostError("the relay connection closed while waiting")

    async def close(self) -> None:
        """Nothing to release."""


@final
class PushRelay(TalkBusRelay):
    """A talk-bus relay that also publishes a wake poke for mail and for a wall."""

    def __init__(self, data_dir: Path, bus: TalkBus) -> None:
        super().__init__(data_dir, bus)
        self._poke_bus = bus
        self.ack_failures = 0  # the next this many acknowledgements fail to publish

    def inbox_notify_subject(self, repo: str, user: str) -> str:
        return f"notify.{repo}.{user}"

    def wall_notify_subject(self, repo: str) -> str:
        return f"wallnotify.{repo}"

    def channel_check_subject(self, session_key: str) -> str:
        return f"channel.check.{session_key}"

    async def deliver(
        self,
        message: Message,
        *,
        sender_key: str = "",
        target_repo: str | None = None,
    ) -> None:
        await super().deliver(message, sender_key=sender_key, target_repo=target_repo)
        if ":" in message.to_user:  # addressed to one session: its talk subject
            await self._poke(self.talk_notify_subject(message.to_user))
        else:
            await self._poke(self.inbox_notify_subject("test", message.to_user))

    async def set_wall(self, wall: WallPost | None) -> None:
        await super().set_wall(wall)
        await self._poke(self.wall_notify_subject("test"))

    async def publish_channel_check_ack(
        self, reply: str, session_key: str, token: str
    ) -> None:
        if self.ack_failures > 0:
            self.ack_failures -= 1
            raise ConnectionError("relay down")
        await self._poke_bus.publish(reply, CheckAck(session_key, token).to_payload())

    async def publish_channel_check_refusal(self, reply: str, reason: str) -> None:
        await self._poke_bus.publish(reply, CheckRefusal(reason).to_payload())

    async def request_channel_check(self, session_key: str) -> FakePending:
        """Publish a doctor's request as the NATS relay does, and wait on its reply."""
        reply = f"_INBOX.{uuid.uuid4().hex}"
        pending = FakePending(session_key)
        await self._poke_bus.subscribe(reply, pending.receive)
        await self._poke_bus.flush()
        await self._poke_bus.publish(
            self.channel_check_subject(session_key), CheckRequest(reply).to_payload()
        )
        return pending

    async def _poke(self, subject: str) -> None:
        """Publish best effort: a lost poke never fails the delivery."""
        try:
            await self._poke_bus.publish(subject, b"1")
        except TimeoutError:
            logger.debug("poke lost")
