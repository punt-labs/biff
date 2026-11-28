"""Tells a missing JetStream stream or KV bucket from an absent message or key."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from nats.errors import Error as NatsError
from nats.js.errors import BucketNotFoundError

if TYPE_CHECKING:
    from nats.js.errors import NotFoundError

__all__ = ["MissingStore", "StoreMissingError"]

_STREAM_NOT_FOUND = 10059
"""The JetStream error code for a stream that does not exist."""


class StoreMissingError(NatsError):
    """The stream or bucket a read needs does not exist, as a purge leaves it."""


@final
class MissingStore:
    """Recognises a missing store and warns once per episode, not once per read.

    A "not found" from NATS means either that the message or key is absent (an
    empty inbox, normal) or that the stream or bucket itself is gone (not
    normal: mail would read as none forever).  Only the second is warned about.
    """

    __slots__ = ("_log", "_warned")

    _log: logging.Logger
    _warned: set[str]

    def __new__(cls, log: logging.Logger) -> Self:
        """Warn through *log*."""
        self = super().__new__(cls)
        self._log = log
        self._warned = set()
        return self

    def gone(self, exc: NotFoundError, store: str) -> bool:
        """Whether *exc* says *store* itself is missing; warn on the first read."""
        is_gone = (
            isinstance(exc, BucketNotFoundError) or exc.err_code == _STREAM_NOT_FOUND
        )
        if is_gone and store not in self._warned:
            self._warned.add(store)
            self._log.warning("%s is missing; reads of it find nothing", store)
        return is_gone

    def found(self, store: str) -> None:
        """Record that *store* answered, so a later loss is a new episode."""
        self._warned.discard(store)
