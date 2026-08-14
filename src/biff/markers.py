"""Workflow marker files bridging async relay state to sync hooks.

MCP tools (plan, wall) store state in the relay (NATS KV or local JSON),
which requires async access.  Hooks run synchronously.  Marker files
bridge this gap: MCP tools write markers as side effects, hooks check
``is_file()`` or ``read_text()`` in <1ms.

Markers are scoped per-repo via SHA-256 hash of the repo-common-root
(the main checkout's path, shared by every linked worktree -- see
``_stdlib.get_repo_common_root``), matching the existing hint-file
architecture (DES-017).  The plan marker additionally carries a session
identity dimension: two concurrent sessions in one repo each get their
own plan-active file, so one session's
``SessionStart`` cannot clear a sibling session's marker out from under
it, and the ``PreToolUse`` gate reads the *caller's own* plan rather than
whichever session happened to write last.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from biff._stdlib import biff_data_dir

logger = logging.getLogger(__name__)

# Claude session_id / agent_id tokens are hex-and-dash UUIDs; this charset
# is deliberately generous (also admits SessionHint-derived hex ids) while
# still refusing anything that could traverse a path (`/`, `..`, etc.).
# An identity that doesn't match falls back to the shared bucket rather
# than being rejected outright -- untrusted hook-payload data degrading to
# "unscoped" is a safe failure mode, not a caller bug worth raising over.
_SAFE_IDENTITY_RE = re.compile(r"^[0-9a-zA-Z_-]{1,128}$")

_SHARED_IDENTITY = "shared"


def _identity_component(identity: str | None) -> str:
    """Return a filesystem-safe path component for *identity*.

    ``None`` (no identity resolvable) and any identity outside the safe
    charset both fall back to the shared bucket -- the plan-gate's
    pre-scoping behavior, preserved for headless/CI/SDK contexts that
    never see a Claude ``session_id`` at all.
    """
    if identity and _SAFE_IDENTITY_RE.match(identity):
        return identity
    logger.debug(
        "identity %r outside safe charset or absent; using shared bucket",
        identity,
    )
    return _SHARED_IDENTITY


def hint_dir(worktree_root: str) -> Path:
    """Repo-scoped hint directory: ``~/.punt-labs/biff/hints/{hash}/``.

    An empty *worktree_root* falls back to the shared ``default`` bucket
    and logs a warning: every unresolved repo lands in the same hash, so
    two concurrent repos with unresolvable roots would share plan/wall
    markers.  Fail-open is deliberate (DES-054 amendment "root-resolution
    failure -- fail-open with observability") -- raising here would break
    every read-only caller (``has_plan_marker``, ``read_wall_marker``) --
    but the warning names the cross-contamination consequence so an
    operator can trace a misrouted marker back to its cause.
    """
    if not worktree_root:
        logger.warning(
            "empty worktree_root; hint markers routed to shared 'default' bucket "
            "(cross-repo contamination risk until git root resolves)",
        )
        return biff_data_dir() / "hints" / "default"
    h = hashlib.sha256(worktree_root.encode()).hexdigest()[:16]
    return biff_data_dir() / "hints" / h


def _plan_marker_path(worktree_root: str, identity: str | None) -> Path:
    """Path to one session identity's plan-active marker."""
    return hint_dir(worktree_root) / "plan" / _identity_component(identity)


def write_plan_marker(worktree_root: str, identity: str | None, plan_text: str) -> None:
    """Write plan-active marker for PreToolUse gate."""
    path = _plan_marker_path(worktree_root, identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plan_text)


def clear_plan_marker(worktree_root: str, identity: str | None) -> None:
    """Remove *identity*'s plan-active marker (plan cleared or session start).

    Removes only this one identity's file -- a concurrent sibling
    session's own marker, keyed under its own identity, is untouched.
    """
    _plan_marker_path(worktree_root, identity).unlink(missing_ok=True)


def has_plan_marker(worktree_root: str, identity: str | None) -> bool:
    """Check whether *identity*'s plan-active marker exists."""
    return _plan_marker_path(worktree_root, identity).is_file()


def read_plan_marker(worktree_root: str, identity: str | None) -> str | None:
    """Read *identity*'s plan-active marker text, or ``None`` if absent."""
    path = _plan_marker_path(worktree_root, identity)
    if not path.is_file():
        return None
    try:
        text = path.read_text().strip()
        return text or None
    except OSError:
        return None


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
