"""biffd's unauthenticated ``GET /health``."""

from __future__ import annotations

import http.client
import json
from dataclasses import dataclass
from typing import Final, Self, cast, final

__all__ = [
    "Health",
    "HealthProbe",
    "HealthProbes",
    "HealthUnavailableError",
    "NotBiffdError",
]

_LOOPBACK: Final = "127.0.0.1"


class HealthUnavailableError(ConnectionError):
    """Nothing on the port answered ``/health`` as biffd does."""


class NotBiffdError(HealthUnavailableError):
    """Something answered ``/health`` clearly, and it is not biffd."""


@dataclass(frozen=True, slots=True)
class Health:
    """What biffd reports about itself without a token."""

    version: str
    uptime_seconds: float
    # A biffd older than the field does not report it; absence is the contract.
    pid: int | None = None
    # What biffd's environment seeded: variable names and relay host, no value.
    # Empty from a biffd older than the field.
    relay_seed: str = ""
    # How the seed changed since biffd last started; empty when it did not.
    relay_seed_note: str = ""


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
            raise NotBiffdError(msg) from exc
        if status != http.client.OK or not isinstance(payload, dict):
            msg = f"{_LOOPBACK}:{self._port} answered /health with status {status}"
            raise NotBiffdError(msg)
        fields = cast("dict[str, object]", payload)
        version = fields.get("version")
        uptime = fields.get("uptime_seconds")
        if not isinstance(version, str) or not isinstance(uptime, int | float):
            msg = f"{_LOOPBACK}:{self._port} is not biffd: /health lacks its fields"
            raise NotBiffdError(msg)
        pid = fields.get("pid")
        seed = fields.get("relay_seed")
        note = fields.get("relay_seed_note")
        return Health(
            version,
            float(uptime),
            pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
            seed if isinstance(seed, str) else "",
            note if isinstance(note, str) else "",
        )


@final
class HealthProbes:
    """Asks each of several loopback ports in turn, and takes the first to answer.

    biffd's recorded port can be stale after a crash, so a client also tries the
    port its unit starts biffd on.
    """

    __slots__ = ("_ports",)

    _ports: tuple[int, ...]

    def __new__(cls, *ports: int) -> Self:
        """Probe *ports* in order; a repeated port is asked once."""
        self = super().__new__(cls)
        self._ports = tuple(dict.fromkeys(ports))
        return self

    def live_port(self, timeout: float) -> int | None:
        """Return the first port where biffd answers within *timeout*, if any.

        ``None`` means no port answers: the absence the caller reports.
        """
        return next(
            (port for port in self._ports if HealthProbe(port).answers(timeout)),
            None,
        )

    def answers(self, timeout: float) -> bool:
        """Return whether biffd answers on any port within *timeout* seconds each."""
        return self.live_port(timeout) is not None
