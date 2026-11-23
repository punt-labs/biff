"""Shared message-delivery orchestration for the MCP tool and CLI command.

Both surfaces send through :func:`deliver_with_retry` so a fix to delivery
semantics (retry policy, failure reporting) applies to both without one
drifting from the other.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nats.errors import Error as NatsError

from biff.models import Message

if TYPE_CHECKING:
    from biff.relay import Relay

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Delivery:
    """Outcome of a delivery: the sender-facing text and whether it succeeded."""

    text: str
    ok: bool
    parts: int


async def deliver_with_retry(
    relay: Relay,
    *,
    from_user: str,
    from_tty: str,
    session_key: str,
    to_user: str,
    target_repo: str | None,
    chunks: list[str],
    display: str,
) -> Delivery:
    """Deliver chunks, retrying once from the first undelivered chunk on failure.

    Returns a :class:`Delivery` carrying the user-facing text and a success
    flag. Delivery is awaited here, not fire-and-forget: a fire-and-forget
    design that returns "Message sent"
    before delivery is even attempted leaves a publish failure — surfaced
    only as a background log line nobody reads — reporting a message as
    arrived when it never did, with no error on either end and no recovery
    path. On a transport failure, retry once from the first
    undelivered chunk rather than restarting from the first chunk —
    mirroring the recovery pattern observed for read_messages (every
    session-reported occurrence cleared on the very next attempt).

    Each chunk's ``Message`` (and therefore its ``id``) is built once, up
    front, and the SAME instance is reused if a retry is needed —
    ``NatsRelay.deliver`` publishes with ``Nats-Msg-Id`` set to that id, so
    JetStream's own deduplication catches the case where the original
    publish actually landed on the server and only its ack was lost to
    the timeout. Constructing a fresh ``Message`` per retry attempt would
    give the retry a different id, defeating that dedup entirely.
    """
    messages = [
        Message(from_user=from_user, from_tty=from_tty, to_user=to_user, body=chunk)
        for chunk in chunks
    ]

    async def _deliver_one(msg: Message) -> None:
        await relay.deliver(msg, sender_key=session_key, target_repo=target_repo)

    delivered = 0
    try:
        for msg in messages:
            await _deliver_one(msg)
            delivered += 1
    except (TimeoutError, NatsError, OSError):
        # Only retry transport-shaped failures. A ValueError from
        # deliver()'s own validation (from_user/to_user/repo) is
        # deterministic — retrying it wastes a round trip and always
        # fails identically, so it is not caught here and propagates as
        # a real error instead of being reported as "failed twice".
        try:
            for msg in messages[delivered:]:
                await _deliver_one(msg)
                delivered += 1
        except Exception:  # noqa: BLE001 — MCP tool / CLI boundary, reported to caller
            # Never interpolate str(exc) into the returned text: this
            # string reaches the model / chat transcript verbatim
            # (/biff:write echoes it), and NatsRelay's own docstrings
            # already flag that exception text from this layer can embed
            # transport URLs or raw frame content.  The log is not the
            # transcript, so it keeps the cause (errno, strerror, traceback).
            _log.warning(
                "Message delivery failed twice (delivered %d/%d parts)",
                delivered,
                len(chunks),
                exc_info=True,
            )
            return Delivery(
                text=(
                    f"Could not deliver to {display} — failed twice. "
                    f"{delivered}/{len(chunks)} part(s) confirmed sent; "
                    "the rest may not have arrived. Not confirmed sent."
                ),
                ok=False,
                parts=len(chunks),
            )
    parts = len(chunks)
    suffix = f" ({parts} parts)" if parts > 1 else ""
    return Delivery(text=f"Message sent to {display}.{suffix}", ok=True, parts=parts)
