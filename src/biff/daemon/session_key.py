"""What identifies a session record: the harness's session id and the caller's role."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from biff.session_id import CLAUDE_CODE_ROLE, CLI_ROLE

if TYPE_CHECKING:
    from biff.frame_client import Hello

__all__ = ["SessionKey"]


@dataclass(frozen=True)
class SessionKey:
    """The pair a session record is kept under.

    A shell and an agent that hold one session id route to different relay keys,
    so they are different sessions with different inboxes and presence rows.
    Keying by the id alone would hand the second one the first one's inbox.
    Every agent-side client (MCP server, hook, plugin) shares the agent's role.
    """

    session_id: str
    role: str

    @classmethod
    def of(cls, hello: Hello) -> Self:
        """Return the key of the session *hello* names."""
        role = CLI_ROLE if hello.client == "cli" else CLAUDE_CODE_ROLE
        return cls(hello.session_id, role)
