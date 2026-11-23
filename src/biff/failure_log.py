"""Log a repeating failure once per episode, and never hide a code bug."""

from __future__ import annotations

import logging
import time
from typing import NamedTuple, Self

import anyio

REMINDER_SECONDS = 600.0

# A peer or client that stopped answering; anything else is a code bug.
TRANSPORT_ERRORS = (
    TimeoutError,
    OSError,
    anyio.BrokenResourceError,
    anyio.ClosedResourceError,
)


class _Episode(NamedTuple):
    """One run of the same failure, by the monotonic clock."""

    started: float
    warned: float
    attempts: int


class EpisodeLog:
    """Log a repeating failure without burying the log or hiding a bug.

    A failure latches per ``(label, exception type)``.  The first one logs the
    cause with its traceback, later ones log at DEBUG, and one reminder per
    ``REMINDER_SECONDS`` names the duration and the attempts, so a failure that
    lasts for hours stays visible.  A failure of an *expected* type is a
    WARNING; any other type is a code bug and logs at ERROR, each distinct type
    on its own, whatever episodes are open.  The label's next success logs one
    WARNING and clears its episodes.  Biff's log keeps INFO and above, so none
    of this rests on DEBUG.
    """

    _logger: logging.Logger
    _expected: tuple[type[BaseException], ...]
    _episodes: dict[tuple[str, type], _Episode]

    def __new__(
        cls, log: logging.Logger, expected: tuple[type[BaseException], ...]
    ) -> Self:
        """Create a log with no failure in progress.

        Args:
            log: The logger the lines are written to.
            expected: Exception types that mean the peer is unreachable.
        """
        self = super().__new__(cls)
        self._logger = log
        self._expected = expected
        self._episodes = {}
        return self

    def failed(self, what: str, exc: Exception) -> None:
        """Record that *what* failed with *exc*."""
        key = (what, type(exc))
        now = time.monotonic()
        is_expected = isinstance(exc, self._expected)
        level = logging.WARNING if is_expected else logging.ERROR
        episode = self._episodes.get(key)
        if episode is None:
            self._episodes[key] = _Episode(now, now, 1)
            retry = "; will retry" if is_expected else ""
            self._logger.log(level, "%s failed: %r%s", what, exc, retry, exc_info=exc)
            return
        episode = episode._replace(attempts=episode.attempts + 1)
        if now - episode.warned < REMINDER_SECONDS:
            self._episodes[key] = episode
            self._logger.debug("%s still failing: %r", what, exc)
            return
        self._episodes[key] = episode._replace(warned=now)
        self._logger.log(
            level,
            "%s still failing for %d min, %d attempts; last error %r",
            what,
            (now - episode.started) // 60,
            episode.attempts,
            exc,
        )

    def recovered(self, what: str) -> None:
        """Record that *what* worked, logging recovery if it had failed."""
        keys = {key for key in self._episodes if key[0] == what}
        if keys:
            self._episodes = {k: e for k, e in self._episodes.items() if k not in keys}
            self._logger.warning("%s recovered", what)
