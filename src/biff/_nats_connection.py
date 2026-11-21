"""NATS connection setup policy and bounded failure diagnostics."""

from __future__ import annotations

import logging
import ssl
from typing import Self

from nats.js.api import KeyValueConfig

from biff.relay import SESSION_TTL_SECONDS

_logger = logging.getLogger("biff.nats_relay")


class NatsConnectionSetup:
    """Build shared bucket settings and diagnose connection attempts safely."""

    _count: int
    _kind: str

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._count = 0
        self._kind = "none"
        return self

    @staticmethod
    def key_value_config(bucket: str, max_bytes: int) -> KeyValueConfig:
        """Use the relay's session lifetime for session and name reservations."""
        return KeyValueConfig(
            bucket=bucket, ttl=SESSION_TTL_SECONDS, max_bytes=max_bytes
        )

    def record(self, exc: Exception) -> None:
        """Keep the onset at INFO and subsequent failures at DEBUG."""
        if isinstance(
            exc, ssl.SSLError
        ) and "APPLICATION_DATA_AFTER_CLOSE_NOTIFY" in str(exc):
            return
        self._kind = type(exc).__name__
        _logger.log(
            logging.DEBUG if self._count else logging.INFO,
            "NATS error (transient, auto-recovering): %s",
            self._kind,
        )
        self._count += 1

    def finish(self, outcome: str, error: str | None = None) -> None:
        """Report recovery or a terminal outcome once, then re-arm the onset."""
        if self._count or outcome == "failed":
            _logger.info(
                "NATS connection outcome=%s failures=%d error=%s",
                outcome,
                self._count,
                error or self._kind,
            )
        self._count = 0
        self._kind = "none"
