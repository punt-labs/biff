"""The one derivation of routing id, session key, and unread path from a session_id."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from biff.session_id import CLAUDE_CODE_ROLE, derive_routing_id
from biff.tty import build_session_key

MAX_ID_LENGTH = 128  # matches markers._SAFE_IDENTITY_RE
ID_RE = re.compile(rf"[A-Za-z0-9_-]{{1,{MAX_ID_LENGTH}}}")


class SessionIdError(ValueError):
    """A session_id is empty, too long, or outside ``[A-Za-z0-9_-]+``."""


@dataclass(frozen=True, slots=True)
class SessionRouting:
    """Everything keyed by a Claude Code ``session_id``.

    The SessionStart hook, the MCP server and the statusline all build one
    of these from the session_id they each see, so the relay row, the unread
    file and the statusline lookup can never disagree about the key.
    """

    session_id: str

    def __post_init__(self) -> None:
        if not ID_RE.fullmatch(self.session_id):
            msg = (
                f"session_id must match [A-Za-z0-9_-]+ and be at most "
                f"{MAX_ID_LENGTH} characters, got {self.session_id!r}"
            )
            raise SessionIdError(msg)

    @property
    def routing_id(self) -> str:
        """Domain-separated routing token for this session."""
        return derive_routing_id(self.session_id, CLAUDE_CODE_ROLE)

    def session_key(self, user: str) -> str:
        """Composite relay key ``{user}:{routing_id}``."""
        return build_session_key(user, self.routing_id)

    def unread_path(self, directory: Path) -> Path:
        """Per-session unread status file inside *directory*.

        Named by the session_id itself, not the digest: the shell nudge hook
        reads it straight from its payload and cannot compute a hash.
        """
        return directory / f"{self.session_id}.json"
