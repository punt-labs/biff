"""Workflow marker files bridging async relay state to sync hooks.

MCP tools (plan, wall) store state in the relay (NATS KV or local JSON),
which requires async access.  Hooks run synchronously.  Marker files
bridge this gap: MCP tools write markers as side effects, hooks check
``is_file()`` or ``read_text()`` in <1ms.

Markers are scoped per-worktree via SHA-256 hash of the worktree root,
matching the existing hint-file architecture (DES-017).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import cast


def hint_dir(worktree_root: str) -> Path:
    """Worktree-scoped hint directory: ``~/.biff/hints/{hash}/``."""
    h = (
        hashlib.sha256(worktree_root.encode()).hexdigest()[:16]
        if worktree_root
        else "default"
    )
    return Path.home() / ".biff" / "hints" / h


def write_plan_marker(worktree_root: str, plan_text: str) -> None:
    """Write plan-active marker for PreToolUse gate."""
    d = hint_dir(worktree_root)
    d.mkdir(parents=True, exist_ok=True)
    (d / "plan-active").write_text(plan_text)


def clear_plan_marker(worktree_root: str) -> None:
    """Remove plan-active marker (plan cleared or session start)."""
    (hint_dir(worktree_root) / "plan-active").unlink(missing_ok=True)


def has_plan_marker(worktree_root: str) -> bool:
    """Check whether a plan-active marker exists."""
    return (hint_dir(worktree_root) / "plan-active").is_file()


def has_bead_in_progress() -> bool:
    """Check whether any bead is in_progress via ``bd list``.

    Returns ``False`` on any error (bd not installed, no .beads/, etc.)
    so the gate defaults to deny-and-explain rather than silent allow.
    """
    try:
        result = subprocess.run(
            ["bd", "list", "--status=in_progress", "-q", "--json"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode != 0:
            return False
        parsed: object = cast("object", json.loads(result.stdout))
        return isinstance(parsed, list) and len(cast("list[object]", parsed)) > 0
    except (FileNotFoundError, json.JSONDecodeError, TimeoutError, OSError):
        return False


# ── Wall markers ─────────────────────────────────────────────────────


def write_wall_marker(worktree_root: str, text: str, expires_at: datetime) -> None:
    """Write wall-active marker with text and expiry."""
    d = hint_dir(worktree_root)
    d.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"text": text, "expires_at": expires_at.isoformat()},
    )
    (d / "wall-active").write_text(payload)


def clear_wall_marker(worktree_root: str) -> None:
    """Remove wall-active marker."""
    (hint_dir(worktree_root) / "wall-active").unlink(missing_ok=True)


def read_wall_marker(worktree_root: str) -> str | None:
    """Read active wall text, or ``None`` if absent/expired."""
    path = hint_dir(worktree_root) / "wall-active"
    if not path.is_file():
        return None
    try:
        data: object = cast("object", json.loads(path.read_text()))
        if not isinstance(data, dict):
            return None
        d = cast("dict[str, object]", data)
        text = d.get("text")
        expires_str = d.get("expires_at")
        if not isinstance(text, str) or not isinstance(expires_str, str):
            return None
        expires = datetime.fromisoformat(expires_str)
        if expires <= datetime.now(UTC):
            path.unlink(missing_ok=True)
            return None
        return text
    except (json.JSONDecodeError, ValueError, TypeError, OSError):
        return None
