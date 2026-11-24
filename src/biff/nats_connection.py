"""NATS connection helpers: wake pokes, URL scrubbing and validation-error scrubbing."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, Self
from urllib.parse import urlsplit

from pydantic import ValidationError

from biff._nats_connection import NatsConnectionSetup
from biff.relay_errors import RelayLog

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class _Publisher(Protocol):
    """The one core-NATS operation a poke needs."""

    async def publish(self, subject: str, payload: bytes) -> None: ...


class NatsSupport(NatsConnectionSetup):
    """Diagnose connection attempts and publish wake pokes, logging their failures.

    A poke only wakes another session's poller, so a failure never reaches the
    caller; but with no timer recomputing the unread count, the inbox poke is
    the one thing that tells a sibling session its count fell.  A relay outage
    logs a WARNING at onset, again every ten minutes, and once on recovery; a
    code bug logs at ERROR.
    """

    _pokes: RelayLog

    def __new__(cls) -> Self:
        """Create a setup with no connection or poke failure in progress."""
        self = super().__new__(cls)
        self._pokes = RelayLog(logging.getLogger("biff.nats_relay"))
        return self

    async def poke(
        self,
        connect: Callable[[], Awaitable[_Publisher]],
        subject: Callable[[], str],
        what: str,
    ) -> None:
        """Publish an empty poke on ``subject()`` through the client *connect* gives."""
        try:
            nc = await connect()
            await nc.publish(subject(), b"1")
            self._pokes.recovered(what)
        except Exception as exc:  # noqa: BLE001 — notification is best-effort
            self._pokes.failed(what, exc)


def sanitize_relay_url(url: str) -> str:
    """Return ``host[:port]`` from the first server in *url*, dropping creds.

    A NATS URL may embed ``user:pass@`` and may be a comma-separated
    cluster list. Never log or embed userinfo in a message -- log lines,
    warnings, and exception text must use this instead of the raw
    (credential-bearing) URL. Unparseable input yields a placeholder
    rather than echoing the raw URL.
    """
    first = url.split(",", 1)[0].strip()
    if "//" not in first:  # urlsplit needs a // authority to find creds/host
        first = "//" + first
    try:
        parts = urlsplit(first)
        host, port = parts.hostname, parts.port
    except ValueError:
        return "<unknown host>"
    if host is None:
        return "<unknown host>"
    if ":" in host:  # IPv6 — bracket regardless of port
        host = f"[{host}]"
    return f"{host}:{port}" if port is not None else host


def scrub_validation_error(exc: ValidationError | ValueError) -> str:
    """Render a validation failure without leaking the frame content.

    ``ValidationError.__str__`` embeds ``input_value`` — the raw frame body,
    which for a talk/mail frame is private plaintext.  Return only the field
    locations and error types, with input, url, and context stripped, so a
    poison-frame log line cannot leak a message body.  A plain ``ValueError``
    (no structured errors) degrades to its type name — never its message.

    Safe only for models whose validation-error ``loc`` cannot contain
    input-derived keys: no ``extra='forbid'`` and no dict-keyed fields.  On such
    a model ``loc`` would carry attacker-controlled key names and this would
    leak input — reuse elsewhere must re-verify that invariant.
    """
    if not isinstance(exc, ValidationError):
        return type(exc).__name__
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc'])}:{err['type']}"
        for err in exc.errors(
            include_input=False, include_url=False, include_context=False
        )
    )
