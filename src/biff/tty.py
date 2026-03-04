"""TTY session identity — generation, naming, and address parsing.

Each biff server instance gets a unique TTY identifier at startup,
analogous to a Unix PTY device name.  Combined with the username,
this forms a session key: ``{user}:{tty}``.
"""

from __future__ import annotations

import re
import secrets
import socket
from pathlib import Path

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
