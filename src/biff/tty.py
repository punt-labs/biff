"""TTY session identity — generation, naming, and address parsing.

Each biff server instance gets a unique TTY identifier at startup,
analogous to a Unix PTY device name.  Combined with the username,
this forms a session key: ``{user}:{tty}``.
"""

from __future__ import annotations

import logging
import re
import secrets
import socket
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from biff.relay import Relay

logger = logging.getLogger(__name__)

_TTY_SEQ_RE = re.compile(r"^tty(\d+)$")
_TTY_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")


def validate_tty_name(name: str) -> str | None:
    """Validate a tty name against the safe character allowlist.

    Returns ``None`` on success, or an error message string on failure.
    TTY names may only contain ASCII alphanumeric characters, hyphens,
    and underscores (1-20 chars).  This prevents terminal escape
    injection when tty names are displayed in notifications and
    ``/read`` output.
    """
    if not _TTY_NAME_RE.match(name):
        return (
            f"Invalid tty name {name!r}: "
            "only letters, digits, hyphens, and underscores are allowed."
        )
    return None


def generate_tty() -> str:
    """Generate an 8-character hex TTY identifier."""
    return secrets.token_hex(4)


def get_hostname() -> str:
    """Return the current hostname."""
    return socket.gethostname()


def get_pwd() -> str:
    """Return the current working directory."""
    return str(Path.cwd())


def build_session_key(user: str, tty: str) -> str:
    """Build a session key from user and tty: ``{user}:{tty}``."""
    return f"{user}:{tty}"


def is_notification_for_session(data: dict[str, str], session_key: str) -> bool:
    """Check whether a talk notification should be accepted by this session.

    Targeted notifications (``to_key`` present) are accepted only when
    the key matches *session_key*.  Broadcast notifications (no
    ``to_key``) are always accepted.
    """
    to_key = data.get("to_key", "")
    return not to_key or to_key == session_key


def next_tty_name(existing_names: list[str]) -> str:
    """Return the lowest ``ttyN`` not already in use."""
    used: set[int] = set()
    for name in existing_names:
        m = _TTY_SEQ_RE.match(name)
        if m:
            used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return f"tty{n}"


_MAX_TTY_CLAIM_RETRIES = 5


async def claim_tty_name(
    relay: Relay,
    user: str,
    session_key: str,
    preferred: str | None = None,
) -> str:
    """Claim a globally unique TTY name via atomic reservation (DES-035).

    Uses ``relay.reserve_tty_name()`` which is backed by NATS KV
    ``create()`` — a server-side CAS operation that succeeds only if
    the key does not already exist.

    If *preferred* is given, attempts to reserve that exact name.
    On collision, raises ``ValueError`` (the caller should show an
    error to the user).

    If *preferred* is ``None``, computes the lowest available ``ttyN``
    from existing reservations and retries on collision up to
    ``_MAX_TTY_CLAIM_RETRIES`` times.

    Returns the reserved name on success.  Raises ``RuntimeError``
    after exhausting retries (should never happen in practice).
    """
    existing = await relay.list_reserved_names(user)

    if preferred is not None:
        candidate = preferred
        ok = await relay.reserve_tty_name(user, candidate, session_key)
        if ok:
            return candidate
        msg = f"name {candidate!r} already in use"
        raise ValueError(msg)

    # Auto-assign: lowest ttyN not in the reserved set.
    candidate = next_tty_name(existing)
    for attempt in range(_MAX_TTY_CLAIM_RETRIES):
        ok = await relay.reserve_tty_name(user, candidate, session_key)
        if ok:
            return candidate
        # Collision — re-enumerate and pick the next candidate.
        logger.debug(
            "TTY name %s collision (attempt %d), retrying",
            candidate,
            attempt + 1,
        )
        existing = await relay.list_reserved_names(user)
        candidate = next_tty_name(existing)

    msg = f"Failed to claim a TTY name after {_MAX_TTY_CLAIM_RETRIES} retries"
    raise RuntimeError(msg)


async def rename_tty(
    relay: Relay,
    user: str,
    session_key: str,
    old_name: str,
    preferred: str | None = None,
) -> str:
    """Claim a new name, then release *old_name* on success.

    Claim-then-release ordering ensures the old name is never lost.
    If the claim fails, the old name remains reserved.
    """
    # Re-establish reservation — it may have lapsed during extended
    # disconnection (e.g., laptop sleep exceeding KV TTL).
    # TTL refresh is handled by heartbeats during normal operation.
    if preferred and preferred == old_name:
        ok = await relay.reserve_tty_name(user, old_name, session_key)
        if not ok:
            # Key exists — verify we still own it before refreshing.
            owner = await relay.get_tty_reservation_owner(user, old_name)
            if owner != session_key:
                msg = f"name {old_name!r} already in use"
                raise ValueError(msg)
            await relay.refresh_tty_reservation(user, old_name, session_key)
        return old_name

    new_name = await claim_tty_name(relay, user, session_key, preferred=preferred)

    # New name claimed — now release old (best-effort).
    if old_name:
        try:
            await relay.release_tty_name(user, old_name)
        except Exception:  # noqa: BLE001
            logger.warning("Failed to release old TTY name %s", old_name)

    return new_name


def parse_address(address: str) -> tuple[str, str | None]:
    """Parse a ``@user`` or ``@user:tty`` address string.

    Returns ``(user, tty)`` where *tty* is ``None`` when not specified.
    Strips leading ``@`` and surrounding whitespace.
    """
    bare = address.strip().lstrip("@")
    if ":" in bare:
        user, tty = bare.split(":", maxsplit=1)
        user = user.strip()
        tty = tty.strip()
        if not tty:
            msg = f"Empty TTY in address: {address!r}"
            raise ValueError(msg)
        return user, tty
    return bare, None
