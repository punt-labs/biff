"""The exception types that mean a relay is unreachable, not that biff has a bug."""

from __future__ import annotations

from nats.errors import Error as NatsError

from biff.failure_log import TRANSPORT_ERRORS

# nats-py errors (closed connection, no servers, stale connection, JetStream
# unavailable) subclass ``nats.errors.Error``, not ``OSError``.
RELAY_ERRORS = (*TRANSPORT_ERRORS, NatsError)
