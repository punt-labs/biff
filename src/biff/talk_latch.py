"""Onset/recovery latch for talk-notification health logging."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Self, final


@final
@dataclass(frozen=True, slots=True)
class LatchMessages:
    """The three phase lines an onset/recovery latch logs.

    ``onset`` is the single WARNING logged when a failure first appears,
    ``retry`` the DEBUG logged per attempt while it persists, and
    ``recovery`` the single INFO logged on the first success afterward.
    """

    onset: str
    retry: str
    recovery: str


_RESUBSCRIBE = LatchMessages(
    onset="Talk notifications are down — re-subscribe failing, retrying each tick",
    retry="Talk re-subscribe still failing, will retry",
    recovery="Talk notifications recovered — re-subscribed",
)
_FETCH = LatchMessages(
    onset="Couldn't read the talk inbox — the relay is unreachable, retrying each tick",
    retry="Talk inbox read still failing, will retry",
    recovery="Talk inbox reachable again — resumed reading",
)


@final
class TalkNotifyLatch:
    """Latch the once-per-episode logging of a failing talk operation.

    Talk health has two independent failure surfaces that a wedged relay can
    trip: re-establishing the always-on talk SUB (poller and REPL idle ticks)
    and reading the durable inbox on the standalone ``biff talk`` fetch tick.
    Both retry every ~2s, so logging each failure at WARNING would flood the
    CLI's WARNING stderr floor with a traceback per tick; logging every failure
    at DEBUG would instead hide a *persistent* outage, because the CLI raises
    the stderr handler to WARNING and DEBUG never reaches it.

    The resolution mirrors ``_ConnectionHealth``'s onset/recovery discipline:
    every failure logs at DEBUG (self-healing, per-attempt), the transition into
    the failing state logs one WARNING, and the first success after any failure
    logs one INFO recovery.  The latch clears on recovery so a later outage
    re-arms it.

    The wording is supplied per flavour via :class:`LatchMessages` — a fetch
    failure names the inbox-read cause, a subscribe failure names the
    re-subscribe cause.  The two are distinct conditions (a healthy SUB can
    coexist with an unreadable inbox) and must not be conflated in the log.
    """

    _logger: logging.Logger
    _messages: LatchMessages
    _failing: bool

    def __new__(cls, logger: logging.Logger, messages: LatchMessages) -> Self:
        self = super().__new__(cls)
        self._logger = logger
        self._messages = messages
        self._failing = False
        return self

    @classmethod
    def for_resubscribe(cls, logger: logging.Logger) -> Self:
        """Return a latch worded for the always-on talk SUB re-subscribe path."""
        return cls(logger, _RESUBSCRIBE)

    @classmethod
    def for_fetch(cls, logger: logging.Logger) -> Self:
        """Return a latch worded for the ``biff talk`` durable-inbox fetch path."""
        return cls(logger, _FETCH)

    def record_failure(self) -> None:
        """Log a failure — WARNING on onset, DEBUG thereafter.

        Call from within an ``except`` block so the log carries ``exc_info``.
        """
        if self._failing:
            self._logger.debug(self._messages.retry, exc_info=True)
            return
        self._failing = True
        self._logger.warning(self._messages.onset, exc_info=True)

    def record_success(self) -> None:
        """Log one INFO recovery on the first success after any failure."""
        if not self._failing:
            return
        self._failing = False
        self._logger.info(self._messages.recovery)
