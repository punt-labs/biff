"""The routing id derived from a Claude Code ``session_id`` and a role."""

from __future__ import annotations

from hashlib import blake2b

CLAUDE_CODE_ROLE = "biff.claude-code.session.v1"
"""Domain separator for routing ids derived from a Claude Code session_id.

The SessionStart hook, the MCP server, and the statusline all derive the
routing id from this one constant; a mismatch would strand the session's
state under a key nobody reads.
"""

_DERIVE_DIGEST_BYTES = 8  # 16 hex chars — inside the routing-id charset


def derive_routing_id(session_id: str, role: str) -> str:
    """Return a stable hex routing id for *role* within *session_id*.

    A deterministic pairing of ``(session_id, role)``: stable across resume
    and distinct per role by construction.  The digest is hex (16 chars),
    inside the routing-token charset ``[0-9a-fA-F-]``, so a
    ``{session_id}:{role}`` composite — whose ``:`` would collide with the
    session-key separator — is never used.
    """
    digest = blake2b(f"{session_id}:{role}".encode(), digest_size=_DERIVE_DIGEST_BYTES)
    return digest.hexdigest()
