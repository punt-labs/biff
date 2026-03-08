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


def next_tty_name(existing_names: list[str]) -> str:
    """Return the next sequential ``ttyN`` not already in use."""
    highest = 0
    for name in existing_names:
        m = _TTY_SEQ_RE.match(name)
        if m:
            highest = max(highest, int(m.group(1)))
    return f"tty{highest + 1}"


_MAX_TTY_RETRIES = 3


async def assign_unique_tty_name(
    relay: Relay,
    session_key: str,
) -> str:
    """Pick a unique ttyN name via read → compute → verify.

    Reads existing sessions, computes the next sequential name,
    and checks that no other session (excluding ours) already
    holds that name.  Retries up to 3 times if a collision is
    detected.

    The caller is responsible for writing the name to KV (via
    ``update_session``).  For full race protection, the caller
    should write, then call :func:`verify_tty_name` to confirm
    no duplicate appeared in the window between compute and write.

    Returns the computed name.  After max retries, returns the
    last computed name (cosmetic duplicate, not worth blocking).
    """
    name = "tty1"
    for attempt in range(_MAX_TTY_RETRIES):
        sessions = await relay.get_sessions()
        existing = [s.tty_name for s in sessions if s.tty_name]
        name = next_tty_name(existing)

        # Check for a pre-existing duplicate (another session that
        # already registered this name before we could).
        duplicates = [
            s
            for s in sessions
            if s.tty_name == name and build_session_key(s.user, s.tty) != session_key
        ]
        if not duplicates:
            return name

        logger.debug(
            "TTY name %s collision (attempt %d), retrying",
            name,
            attempt + 1,
        )

    return name


async def verify_tty_name(
    relay: Relay,
    session_key: str,
    name: str,
) -> str:
    """Verify tty name uniqueness after writing to KV.

    Re-reads sessions and checks for duplicates.  If another
    session grabbed the same name concurrently, picks the next
    available name and updates our session in KV.

    Call this after ``update_session`` to close the TOCTOU window.
    Returns the final (possibly updated) name.
    """
    for _attempt in range(_MAX_TTY_RETRIES):
        sessions = await relay.get_sessions()
        duplicates = [
            s
            for s in sessions
            if s.tty_name == name and build_session_key(s.user, s.tty) != session_key
        ]
        if not duplicates:
            return name

        # Collision — pick next name and re-register.
        existing = [s.tty_name for s in sessions if s.tty_name]
        name = next_tty_name(existing)
        session = await relay.get_session(session_key)
        if session is not None:
            updated = session.model_copy(update={"tty_name": name})
            await relay.update_session(updated)
        logger.debug("TTY name collision, reassigned to %s", name)

    return name


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
