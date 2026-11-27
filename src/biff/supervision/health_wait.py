"""How long a start waits for biffd to answer ``/health``."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Final, Self, final

from biff.supervision.errors import ServiceActionFailedError

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["HealthWait"]

_MAX_SECONDS: Final = 600.0


@final
@dataclass(frozen=True, slots=True)
class HealthWait:
    """A positive number of seconds, read from ``BIFF_DAEMON_HEALTH_WAIT``.

    biffd imports its whole relay stack before it listens: 0.8 s warm and 1.8 s
    with cold bytecode on an idle 24-core machine, 2.3 s with every core busy.
    A laptop at a load average of 229 took longer than the 5 seconds an install
    once allowed, so install, start and restart wait 30 seconds by default.
    """

    ENVIRONMENT_VARIABLE: ClassVar[str] = "BIFF_DAEMON_HEALTH_WAIT"
    DEFAULT_SECONDS: ClassVar[float] = 30.0

    seconds: float

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> Self:
        """Return the wait *environ* asks for, or the default when it is silent.

        Raises:
            ServiceActionFailedError: The variable is not a number of seconds
                between 0 (exclusive) and 600.
        """
        name = cls.ENVIRONMENT_VARIABLE
        raw = environ.get(name)
        if raw is None or not raw.strip():
            return cls(cls.DEFAULT_SECONDS)
        try:
            seconds = float(raw)
        except ValueError:
            seconds = math.nan
        if not 0 < seconds <= _MAX_SECONDS:
            msg = (
                f"{name} must be a number of seconds above 0 and at most "
                f"{_MAX_SECONDS:g}, not {raw!r}"
            )
            raise ServiceActionFailedError(msg)
        return cls(seconds)
