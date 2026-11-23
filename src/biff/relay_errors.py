"""The exception types that mean a relay is unreachable, not that biff has a bug."""

from __future__ import annotations

import logging
from typing import Self

from nats.errors import Error as NatsError

from biff.failure_log import TRANSPORT_ERRORS, EpisodeLog

# nats-py errors (closed connection, no servers, stale connection, JetStream
# unavailable) subclass ``nats.errors.Error``, not ``OSError``.
RELAY_ERRORS = (*TRANSPORT_ERRORS, NatsError)


class RelayLog(EpisodeLog):
    """An ``EpisodeLog`` for work that talks to a relay: an outage is expected."""

    def __new__(cls, log: logging.Logger) -> Self:
        """Create a log that warns about relay outages and errors on bugs."""
        return super().__new__(cls, log, RELAY_ERRORS)
