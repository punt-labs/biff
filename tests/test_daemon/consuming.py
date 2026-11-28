"""A local relay that consumes mail at fetch, as the NATS relay does."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path

from biff.models import Message, UnreadSummary
from biff.relay import LocalRelay

__all__ = ["ConsumingRelay"]


class ConsumingRelay(LocalRelay):
    """A relay faithful to ``NatsRelay`` fetch semantics: fetching consumes.

    ``NatsRelay.fetch`` acks every frame it pulls, a work queue deletes an acked
    frame, and ``mark_read`` is a no-op because the mail is already gone.
    """

    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)

    consumes_on_fetch = True

    async def get_unread_summary(self, session_key: str) -> UnreadSummary:
        """Count without consuming: the NATS relay counts from stream info."""
        tty = await LocalRelay.fetch(self, session_key)
        user = await LocalRelay.fetch_user_inbox(self, session_key.split(":")[0])
        return UnreadSummary(count=len(tty) + len(user))

    async def fetch(self, session_key: str) -> list[Message]:
        unread = await super().fetch(session_key)
        await super().mark_read(session_key, [m.id for m in unread])
        return unread

    async def fetch_user_inbox(self, user: str) -> list[Message]:
        unread = await super().fetch_user_inbox(user)
        await super().mark_read_user_inbox(user, [m.id for m in unread])
        return unread

    async def mark_read(self, session_key: str, ids: Sequence[uuid.UUID]) -> None:
        return None

    async def mark_read_user_inbox(self, user: str, ids: Sequence[uuid.UUID]) -> None:
        return None
