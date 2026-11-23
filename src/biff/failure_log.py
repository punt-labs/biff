"""Logging for failures that repeat: onset, quiet retries, reminders, recovery.

Shaped like ``EpisodeLog`` in the channel-mode work (``failed(what, exc)``,
``recovered(what)``, an *expected* exception tuple, a reminder interval) so the
two merge into one helper.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Callable


class FailureLog:
    """Log each distinct failing thing once, remind rarely, and log recovery.

    A failure of *what* is logged when it starts (WARNING for an *expected*
    exception type, ERROR with a traceback for anything else).  Retries log at
    DEBUG, which production discards, so a failure lasting days would otherwise
    leave one line; a WARNING reminder is therefore logged every *remind_every*
    seconds.  Recovery is logged once at WARNING.
    """

    __slots__ = ("_clock", "_episodes", "_expected", "_logger", "_remind_every")

    _logger: logging.Logger
    _expected: tuple[type[BaseException], ...]
    _remind_every: float
    _clock: Callable[[], float]
    _episodes: dict[str, list[float]]  # what -> [attempts, since, last logged]

    def __new__(
        cls,
        logger: logging.Logger,
        *,
        expected: tuple[type[BaseException], ...] = (),
        remind_every: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> Self:
        """Create a log that reminds every *remind_every* seconds."""
        self = super().__new__(cls)
        self._logger = logger
        self._expected = expected
        self._remind_every = remind_every
        self._clock = clock
        self._episodes = {}
        return self

    def failing(self, what: str) -> bool:
        """Whether the last attempt at *what* failed."""
        return what in self._episodes

    def failed(self, what: str, exc: BaseException) -> None:
        """Record a failed attempt at *what*, logging it when it is worth a line."""
        now = self._clock()
        code = getattr(exc, "errno", None)
        cause = f"{exc} (errno {code})" if code else str(exc)
        episode = self._episodes.get(what)
        if episode is None:
            self._episodes[what] = [1, now, now]
            expected = isinstance(exc, self._expected)
            self._logger.log(
                logging.WARNING if expected else logging.ERROR,
                "%s failed: %s",
                what,
                cause,
                exc_info=None if expected else exc,
            )
            return
        episode[0] += 1
        if now - episode[2] >= self._remind_every:
            episode[2] = now
            self._logger.warning(
                "%s still failing after %d attempts, %.0f min: %s",
                what,
                episode[0],
                (now - episode[1]) / 60,
                cause,
            )
        else:
            self._logger.debug("%s failed again: %s", what, cause)

    def recovered(self, what: str) -> None:
        """Log recovery of *what* once, if it had been failing."""
        episode = self._episodes.pop(what, None)
        if episode is not None:
            self._logger.warning(
                "%s recovered after %d failed attempts", what, episode[0]
            )
