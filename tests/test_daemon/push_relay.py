"""A relay that pushes arrival notices over an in-memory bus, as the NATS relay does."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, final

from tests.test_daemon.talk_bus import TalkBusRelay

if TYPE_CHECKING:
    from pathlib import Path

    from biff.models import Message, WallPost
    from tests.test_daemon.talk_bus import TalkBus

logger = logging.getLogger(__name__)


@final
class PushRelay(TalkBusRelay):
    """A talk-bus relay that also publishes a wake poke for mail and for a wall."""

    def __init__(self, data_dir: Path, bus: TalkBus) -> None:
        super().__init__(data_dir, bus)
        self._poke_bus = bus

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

    async def _poke(self, subject: str) -> None:
        """Publish best effort: a lost poke never fails the delivery."""
        try:
            await self._poke_bus.publish(subject, b"1")
        except TimeoutError:
            logger.debug("poke lost")
