"""biffd's unauthenticated ``GET /health``."""

from __future__ import annotations

import http.client
import json
from dataclasses import dataclass
from typing import Final, Self, cast, final

__all__ = ["Health", "HealthProbe", "HealthUnavailableError"]

_LOOPBACK: Final = "127.0.0.1"


class HealthUnavailableError(ConnectionError):
    """Nothing on the port answered ``/health`` as biffd does."""


@dataclass(frozen=True, slots=True)
class Health:
    """What biffd reports about itself without a token."""

    version: str
    uptime_seconds: float


@final
class HealthProbe:
    """Asks the listener on one loopback port for its health."""

    __slots__ = ("_port",)

    _port: int

    def __new__(cls, port: int) -> Self:
        """Probe the listener on *port*."""
        self = super().__new__(cls)
        self._port = port
        return self

    def fetch(self, timeout: float) -> Health:
        """Return biffd's health, waiting at most *timeout* seconds.

        Raises:
            HealthUnavailableError: The connection failed, timed out, or the
                listener's answer is not biffd's.
        """
        conn = http.client.HTTPConnection(_LOOPBACK, self._port, timeout=timeout)
        try:
            conn.request("GET", "/health")
            response = conn.getresponse()
            body = response.read()
            status = response.status
        except (OSError, http.client.HTTPException) as exc:
            msg = f"nothing answers /health on {_LOOPBACK}:{self._port} ({exc})"
            raise HealthUnavailableError(msg) from exc
        finally:
            conn.close()
        return self._parse(status, body)

    def answers(self, timeout: float) -> bool:
        """Return whether biffd answers ``/health`` within *timeout* seconds."""
        try:
            self.fetch(timeout)
        except HealthUnavailableError:
            return False
        return True

    def _parse(self, status: int, body: bytes) -> Health:
        """Read biffd's answer, or refuse a listener that is something else."""
        try:
            payload: object = json.loads(body)
        except ValueError as exc:
            msg = f"{_LOOPBACK}:{self._port} did not answer /health with JSON"
            raise HealthUnavailableError(msg) from exc
        if status != http.client.OK or not isinstance(payload, dict):
            msg = f"{_LOOPBACK}:{self._port} answered /health with status {status}"
            raise HealthUnavailableError(msg)
        fields = cast("dict[str, object]", payload)
        version = fields.get("version")
        uptime = fields.get("uptime_seconds")
        if not isinstance(version, str) or not isinstance(uptime, int | float):
            msg = f"{_LOOPBACK}:{self._port} is not biffd: /health lacks its fields"
            raise HealthUnavailableError(msg)
        return Health(version, float(uptime))
