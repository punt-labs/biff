"""Onset/recovery latch for the always-on talk re-subscribe logging."""

from __future__ import annotations

import logging
from typing import Self, final


@final
class TalkResubscribeLatch:
    """Latch the once-per-episode logging of a failing talk re-subscribe.

    The always-on talk SUB is re-established every poller tick (~2s) and every
    REPL idle tick.  When NATS is down the re-subscribe fails on every tick.
    Logging each failure at WARNING would flood the CLI's WARNING stderr floor
    with a traceback per tick; logging every failure at DEBUG would instead
    hide a *persistent* talk-down condition, because the CLI raises the stderr
    handler to WARNING and DEBUG never reaches it.

    The resolution mirrors ``_ConnectionHealth``'s onset/recovery discipline:
    every failure logs at DEBUG (self-healing, per-attempt), the transition
    into the failing state logs one WARNING, and the first success after any
    failure logs one INFO recovery.  The latch clears on recovery so a later
    outage re-arms it.
    """

    _logger: logging.Logger
    _failing: bool

    def __new__(cls, logger: logging.Logger) -> Self:
        self = super().__new__(cls)
        self._logger = logger
        self._failing = False
        return self

    def record_failure(self) -> None:
        """Log a re-subscribe failure — WARNING on onset, DEBUG thereafter.

        Call from within an ``except`` block so the log carries ``exc_info``.
        """
        if self._failing:
            self._logger.debug(
                "Talk re-subscribe still failing, will retry", exc_info=True
            )
            return
        self._failing = True
        self._logger.warning(
            "Talk notifications are down — re-subscribe failing, retrying each tick",
            exc_info=True,
        )

    def record_success(self) -> None:
        """Log one INFO recovery on the first success after any failure."""
        if not self._failing:
            return
        self._failing = False
        self._logger.info("Talk notifications recovered — re-subscribed")
