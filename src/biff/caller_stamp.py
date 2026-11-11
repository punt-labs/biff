"""Stamp the harness-declared ``_caller`` onto biff's own tool calls."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from biff.session_routing import ID_RE

# Every name Claude Code gives biff's server: plugin, -dev plugin, direct.
BIFF_TOOL_RE = re.compile(r"mcp__(plugin_biff(-dev)?_)?tty__.*")
MATCHER = BIFF_TOOL_RE.pattern


def main_agent_id(
    environ: Mapping[str, str], *, ancestors: frozenset[int] | None = None
) -> str | None:
    """Return the main-thread agent id, or ``None`` when it cannot be trusted.

    It is ``CLAUDE_PID``, accepted only when that process is a live ancestor
    of this hook.  A recycled pid, or an environment that never carried one,
    is not an identity.  This corroborates a value the harness supplied; it
    never discovers one.
    """
    raw = environ.get("CLAUDE_PID", "")
    if not raw.isdecimal():
        return None
    live = _live_ancestors() if ancestors is None else ancestors
    return raw if int(raw) in live else None


def _live_ancestors() -> frozenset[int]:
    import psutil  # noqa: PLC0415

    return frozenset(p.pid for p in psutil.Process().parents())


def _deny(reason: str) -> dict[str, object]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _updated(tool_input: dict[str, object]) -> dict[str, object]:
    # No permissionDecision: an "allow" here would bypass the user's rules.
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": tool_input,
        }
    }


def stamp_failure(tool: str, exc: Exception) -> dict[str, object]:
    """Deny a biff tool call whose stamping crashed, naming the cause."""
    return _deny(f"biff stamping hook failed for {tool}: {exc}")


@dataclass(frozen=True, slots=True)
class CallerStamper:
    """Builds the PreToolUse response that declares who is calling.

    ``main_agent_id`` is the corroborated main-thread id, or ``None`` when
    it could not be established.
    """

    main_agent_id: str | None

    def stamp(self, payload: dict[str, object]) -> dict[str, object] | None:
        """Return the hook response for *payload*, or ``None`` to leave it alone."""
        tool = payload.get("tool_name")
        if not isinstance(tool, str):
            return _deny("biff stamping hook could not read the payload: no tool_name")
        if not BIFF_TOOL_RE.fullmatch(tool):
            return None
        args = payload.get("tool_input")
        if not isinstance(args, dict):
            return _deny(
                f"biff stamping hook could not read the payload for {tool}: "
                "tool_input is not an object"
            )
        fields = cast("dict[str, object]", args)
        updated: dict[str, object] = {k: v for k, v in fields.items() if k != "_caller"}
        caller = self._caller(payload)
        if caller is not None:
            updated["_caller"] = caller
        return _updated(updated)

    def _caller(self, payload: dict[str, object]) -> dict[str, str] | None:
        session_id = payload.get("session_id")
        sub = payload.get("agent_id")
        agent_id = sub if isinstance(sub, str) and sub else self.main_agent_id
        if not isinstance(session_id, str) or agent_id is None:
            return None
        if not ID_RE.fullmatch(session_id) or not ID_RE.fullmatch(agent_id):
            return None
        return {"session_id": session_id, "agent_id": agent_id}
