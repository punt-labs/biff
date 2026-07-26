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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from biff._stdlib import biff_data_dir


def hint_dir(worktree_root: str) -> Path:
    """Worktree-scoped hint directory: ``~/.punt-labs/biff/hints/{hash}/``."""
    h = (
        hashlib.sha256(worktree_root.encode()).hexdigest()[:16]
        if worktree_root
        else "default"
    )
    return biff_data_dir() / "hints" / h


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


def read_plan_marker(worktree_root: str) -> str | None:
    """Read the plan-active marker text, or ``None`` if absent."""
    path = hint_dir(worktree_root) / "plan-active"
    if not path.is_file():
        return None
    try:
        text = path.read_text().strip()
        return text or None
    except OSError:
        return None


# How long a validated bead claim is trusted before the gate re-queries
# ``bd``.  The marker is a perf cache, not ground truth: a bead closed
# out-of-band (direct Dolt, another session) leaves a stale marker, so
# the gate must re-validate once the stamp ages past this bound.  Short
# enough to bound the phantom-claim window; long enough that a burst of
# edits after a claim never pays for a subprocess (biff-84a).
_BEAD_MARKER_TTL = timedelta(seconds=60)


def write_bead_marker(worktree_root: str) -> None:
    """Write bead-active marker stamped with the current validation time.

    The timestamp bounds how long the PreToolUse gate trusts the marker
    without re-querying ``bd`` (see ``_BEAD_MARKER_TTL``).
    """
    d = hint_dir(worktree_root)
    d.mkdir(parents=True, exist_ok=True)
    (d / "bead-active").write_text(datetime.now(UTC).isoformat())


def clear_bead_marker(worktree_root: str) -> None:
    """Remove bead-active marker (bead closed — force re-check on next gate)."""
    (hint_dir(worktree_root) / "bead-active").unlink(missing_ok=True)


def _bead_marker_fresh(marker: Path) -> bool:
    """True if *marker* holds a timestamp validated within the TTL.

    A missing, unparseable, naive, or expired stamp is not fresh, so the
    caller re-validates against ``bd`` rather than trusting the cache.
    """
    try:
        stamped = datetime.fromisoformat(marker.read_text().strip())
    except (OSError, ValueError):
        return False
    if stamped.tzinfo is None:
        return False
    return datetime.now(UTC) - stamped < _BEAD_MARKER_TTL


type BeadStatus = Literal["yes", "no", "unavailable"]


def check_bead_in_progress(worktree_root: str = "") -> BeadStatus:
    """Check whether any bead is in_progress.

    Fast path: a ``bead-active`` marker stamped within ``_BEAD_MARKER_TTL``
    is trusted without touching ``bd`` (<1ms).  Slow path: an absent or
    stale marker triggers a ``bd list`` subprocess, then the marker is
    reconciled with the truth — refreshed on ``"yes"``, dropped on
    ``"no"`` (a phantom claim closed out-of-band), left untouched on
    ``"unavailable"`` (a transient outage re-checks next time).

    Returns ``"yes"`` if at least one bead is claimed, ``"no"`` if the
    list is empty, or ``"unavailable"`` if ``bd`` is not installed,
    times out, or otherwise fails.
    """
    marker = hint_dir(worktree_root) / "bead-active" if worktree_root else None

    # Fast path: a recently-validated claim is trusted without querying bd.
    if marker is not None and _bead_marker_fresh(marker):
        return "yes"

    # Slow path: marker absent or stale — query bd and reconcile the cache.
    status = _check_bead_subprocess()
    if marker is not None:
        if status == "yes":
            write_bead_marker(worktree_root)  # refresh the stamp
        elif status == "no":
            marker.unlink(missing_ok=True)  # drop the phantom claim
        # "unavailable": leave the marker; the outage is transient.
    return status


def _check_bead_subprocess() -> BeadStatus:
    """Check bead status via ``bd list`` subprocess (slow path)."""
    try:
        result = subprocess.run(
            ["bd", "list", "--status=in_progress", "-q", "--json"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode != 0:
            return "unavailable"
        parsed: object = cast("object", json.loads(result.stdout))
        if isinstance(parsed, list) and len(cast("list[object]", parsed)) > 0:
            return "yes"
        return "no"
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        subprocess.TimeoutExpired,
        OSError,
    ):
        return "unavailable"


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
