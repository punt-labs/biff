"""The plan-gate marker: the file the edit gate reads to see a session has a plan."""

from __future__ import annotations

import os
from typing import final

from biff._stdlib import get_repo_common_root
from biff.markers import clear_plan_marker, write_plan_marker
from biff.session_routing import ID_RE

__all__ = ["PlanMarker"]


@final
class PlanMarker:
    """What the process that runs ``plan`` does to the filesystem.

    The marker is keyed on the caller's repository and harness session, and a
    bead id expands against the caller's repository, so only the calling
    process can do either; a daemon that runs the engine's ``plan`` cannot.
    Callers expand a bead id with ``biff._stdlib.expand_bead_id`` first.
    """

    @staticmethod
    def declared_session() -> str | None:
        """Return the session Claude Code declares, or None when absent.

        Claude Code sets ``CLAUDE_CODE_SESSION_ID`` in the environment of each
        Bash tool call.  It is the harness stating the session, read only by the
        CLI, which has no ``_caller``; the server and hooks never read it.  A
        value outside the identity charset is treated as absent.
        """
        value = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        return value if ID_RE.fullmatch(value) else None

    @staticmethod
    def sync(message: str, session_id: str | None = None) -> None:
        """Write or clear the plan-gate marker for *session_id*.

        The MCP tool passes the session the caller declared.  The CLI passes the
        Claude Code session when :meth:`declared_session` finds one (the
        edit-gate hook reads that id), else its stored session's id, so its
        marker is scoped to the session that ran it: another session's edit gate
        never reads it, and that session's SessionStart clears it.  With no
        session at all (headless) the marker goes to the shared bucket, which
        only an identity-less payload reads.  The root is resolved fresh on
        every call so a long-running server and a one-shot CLI invocation agree
        with the hook.
        """
        root = get_repo_common_root()
        identity = (
            session_id if session_id is not None else PlanMarker.declared_session()
        )
        if message:
            write_plan_marker(root, identity, message)
        else:
            clear_plan_marker(root, identity)
