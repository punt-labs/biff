"""A session's presence heartbeat, carrying the plugin hook's activity stamp."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self, final

from biff.daemon.activity_stamp import ActivityStamp
from biff.failure_log import EpisodeLog
from biff.relay_errors import RELAY_ERRORS, RelayLog

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from biff.relay import Relay

__all__ = ["Heartbeat"]

logger = logging.getLogger(__name__)


@final
class Heartbeat:
    """The one heartbeat a session sends while a client is attached.

    Each beat refreshes the row's ``last_active`` and hands the relay the time
    the hook last stamped, so ``last_tool_at`` (the idle time every other host
    shows) follows tool calls that never touch biff.  The relay advances it
    inside its own read-modify-write, forward only; a plain heartbeat would show
    a session that works through Bash and Edit as hours idle.

    Presence does not depend on the stamp: a stamp that cannot be read is logged
    once per episode and the beat goes out without it.
    """

    __slots__ = ("_interval", "_key", "_log", "_relay", "_stamp", "_stamp_log")

    _relay: Relay
    _key: str
    _stamp: ActivityStamp
    _interval: float
    _log: RelayLog
    _stamp_log: EpisodeLog

    def __new__(cls, relay: Relay, key: str, session_id: str, interval: float) -> Self:
        """Beat the row *key* of *relay* every *interval* seconds.

        The stamp is the one named by *session_id*.
        """
        self = super().__new__(cls)
        self._relay = relay
        self._key = key
        self._stamp = ActivityStamp(session_id)
        self._interval = interval
        self._log = RelayLog(logger)
        self._stamp_log = EpisodeLog(logger, (OSError,))
        return self

    async def run(self, after_each: Callable[[], Awaitable[None]]) -> None:
        """Beat for ever, awaiting *after_each* once per beat.

        A relay failure is logged once per outage and never ends the loop.
        """
        while True:
            await asyncio.sleep(self._interval)
            stamped = self._stamped_at()
            try:
                await self._relay.heartbeat(self._key, stamped)
            except RELAY_ERRORS as exc:
                self._log.failed("heartbeat", exc)
            else:
                self._log.recovered("heartbeat")
            await after_each()

    def _stamped_at(self) -> datetime | None:
        """Return the stamp's time, or ``None`` when there is none to read."""
        try:
            at = self._stamp.touched_at()
        except OSError as exc:
            self._stamp_log.failed("activity stamp", exc)
            return None
        self._stamp_log.recovered("activity stamp")
        return at
