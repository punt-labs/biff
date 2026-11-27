"""One session's talk: the feed from the relay and the notices it raises."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self, final

import biff.commands.talk as talk_commands
from biff.daemon.talk_feed import TalkFeed
from biff.daemon.talk_notices import TalkNotices
from biff.talk_types import TalkPhase

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pydantic import JsonValue

    from biff.commands.talk import TalkContext
    from biff.frames import EventKind
    from biff.relay import Relay
    from biff.talk_state import TalkState

__all__ = ["TalkChannel"]

logger = logging.getLogger(__name__)


@final
class TalkChannel:
    """Runs a session's talk subscription and tells listeners what arrives."""

    __slots__ = ("_feed", "_notices")

    _feed: TalkFeed
    _notices: TalkNotices

    def __new__(
        cls,
        *,
        relay: Relay,
        session_key: str,
        talk: TalkState,
        interval: float,
    ) -> Self:
        """Create the channel of the session *session_key*, held in *talk*.

        *interval* is how often the subscription is checked against the relay's
        connection.
        """
        self = super().__new__(cls)
        self._notices = TalkNotices()
        self._feed = TalkFeed(
            relay=relay,
            session_key=session_key,
            talk=talk,
            notices=self._notices,
            interval=interval,
        )
        return self

    @staticmethod
    async def hang_up(ctx: TalkContext) -> None:
        """End the talk, or withdraw the invite, that *ctx*'s session still holds.

        A session whose last client left mid-talk would otherwise stay in the
        conversation for ever: its peer's lines would go to a subject nobody
        reads and its next talk would be refused.  The kernel resets the state
        first and publishes best effort, so this never raises for a relay outage.
        """
        if ctx.talk.phase is not TalkPhase.IDLE:
            await talk_commands.end_or_cancel(ctx)

    async def ready(self, timeout: float) -> None:
        """Return once frames for this session are being received.

        A relay that is slow to subscribe must not fail an attach: after
        *timeout* seconds this logs and returns, the feed keeps trying, and a
        client that talks in the meantime is told so by the relay.
        """
        try:
            await asyncio.wait_for(self._feed.ready(), timeout=timeout)
        except TimeoutError:
            logger.warning("talk subscription not ready; the attach goes on")

    def start(self) -> None:
        """Subscribe while a client is attached; harmless when already running."""
        self._feed.start()

    def stop(self) -> None:
        """Release the subscription."""
        self._feed.stop()

    async def announce(
        self, emit: Callable[[EventKind, JsonValue], Awaitable[None]]
    ) -> None:
        """Pass *emit* each notice that arrives, until cancelled.

        Only frames that arrive while this runs are announced; a client that
        starts late collects what is held with ``talk_read``.
        """
        with self._notices.listen() as notices:
            while True:
                notice = await notices.get()
                await emit(notice.kind, notice.data)
