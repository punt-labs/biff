"""Hook dispatcher for biff lifecycle events (DES-017).

All hook shell scripts delegate to ``biff hook <layer> <event>``.
Business logic lives here in versioned Python; shell scripts are
thin dispatchers with only a fast ``config.local.yaml`` existence gate.

Layer 1: Claude Code hooks — capture agent lifecycle events.
Layer 2: Git hooks — capture code lifecycle events.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import select
import subprocess
import sys
from contextlib import suppress
from typing import cast

import typer

logger = logging.getLogger(__name__)

# ── CLI structure ────────────────────────────────────────────────────

hook_app = typer.Typer(
    help="Hook dispatchers (called by hook scripts, not by users).",
    no_args_is_help=True,
)
_cc_app = typer.Typer(
    help="Claude Code lifecycle hooks.",
    no_args_is_help=True,
)
_git_app = typer.Typer(
    help="Git lifecycle hooks.",
    no_args_is_help=True,
)
hook_app.add_typer(_cc_app, name="claude-code")
hook_app.add_typer(_git_app, name="git")


# ── Shared helpers ───────────────────────────────────────────────────


def _is_biff_enabled() -> bool:
    """Check ``config.local.yaml`` enabled gating (lazy import)."""
    from biff._stdlib import find_git_root, is_enabled  # noqa: PLC0415

    repo_root = find_git_root()
    return repo_root is not None and is_enabled(repo_root)


def _has_beads() -> bool:
    """Check whether beads is available (``.beads/`` exists in git root)."""
    from biff._stdlib import find_git_root  # noqa: PLC0415

    repo_root = find_git_root()
    return repo_root is not None and (repo_root / ".beads").is_dir()


def _is_lux_enabled() -> bool:
    """Check whether lux display mode is enabled (delegates to _stdlib)."""
    from biff._stdlib import is_lux_enabled  # noqa: PLC0415

    return is_lux_enabled()


def _read_hook_input() -> dict[str, object]:
    """Read JSON hook payload from stdin (non-blocking).

    Uses ``select`` + ``os.read`` to avoid blocking forever when the
    caller does not close the stdin pipe.  Never calls
    ``sys.stdin.read()`` which blocks until EOF.

    Strategy: wait up to 100ms for initial data, then read available
    bytes in chunks with a 50ms inter-chunk timeout.  Stops as soon
    as no more data arrives — does not require EOF.
    """
    try:
        fd = sys.stdin.fileno()
        # Wait up to 100ms for initial data.
        if not select.select([fd], [], [], 0.1)[0]:
            return {}
        # Read available data in chunks (50ms inter-chunk timeout).
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:  # EOF
                break
            chunks.append(chunk)
            if not select.select([fd], [], [], 0.05)[0]:
                break
        raw = b"".join(chunks).decode()
        if not raw.strip():
            return {}
        data = json.loads(raw)
        if isinstance(data, dict):
            return cast("dict[str, object]", data)
        return {}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def _emit(output: dict[str, object]) -> None:
    """Write JSON hook response to stdout."""
    json.dump(output, sys.stdout)
    sys.stdout.write("\n")


def _hook_context(event: str, context: str) -> dict[str, object]:
    """Build hook output with ``additionalContext`` for any event."""
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": context,
        }
    }


def _post_tool_use_context(context: str) -> dict[str, object]:
    """Build PostToolUse hook output with ``additionalContext`` only."""
    return _hook_context("PostToolUse", context)


def _parse_tool_response(raw: object) -> dict[str, object]:
    """Parse ``tool_response`` which may be a JSON string or dict."""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return cast("dict[str, object]", parsed)
        except json.JSONDecodeError:
            pass
        return {}
    if isinstance(raw, dict):
        return cast("dict[str, object]", raw)
    return {}


# ── Handlers (pure functions, testable without I/O) ──────────────────


def _pre_tool_use_deny(reason: str) -> dict[str, object]:
    """Build a PreToolUse hard-deny response carrying *reason*.

    ``permissionDecision: "deny"`` blocks the Edit/Write outright and
    feeds *reason* back to the model via ``permissionDecisionReason``
    (DES-026) — the field name Claude Code actually reads.  Unlike
    ``ask`` (DES-031) a deny never raises a user-facing permission
    prompt; the block is silent to the operator and the model
    self-corrects by following the instructions in *reason*.
    """
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _has_active_session() -> bool:
    """True if any biff MCP server session is active."""
    from biff._stdlib import active_dir  # noqa: PLC0415

    adir = active_dir()
    if not adir.is_dir():
        return False
    return any(f.is_file() for f in adir.iterdir())


def _resolve_identity(data: dict[str, object]) -> str | None:
    """Resolve the caller's preferred plan-gate identity from a hook payload.

    Prefers ``agent_id`` over ``session_id`` when both are present: an
    in-process dispatched subagent shares its leader's OS ``claude``
    process (and therefore the leader's ``session_id`` fallback via
    ``SessionHint``), but Claude Code delivers the subagent's *own*
    ``agent_id`` on its tool-call payloads, distinct from the leader's
    top-level ``session_id``.  ``None`` when
    neither is present — headless/CI/SDK contexts with no Claude session
    at all — and the marker degrades to the shared, unscoped bucket.
    """
    for key in ("agent_id", "session_id"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _identity_candidates(data: dict[str, object]) -> tuple[str | None, ...]:
    """Return this caller's own identities in gate-read preference order.

    The gate's *write* path is scoped to a single identity — the caller's
    preferred one from :func:`_resolve_identity`, so a ``SessionStart``
    can never wipe a sibling's marker (om9's core invariant). The *read*
    path, in contrast, walks a preference list: for a dispatched
    in-process subagent whose ``Bash``-invoked ``biff plan`` wrote under
    the leader's ``session_id`` (no channel today for a subagent's
    ``Bash`` tool to learn its own ``agent_id``), a subsequent
    ``PreToolUse`` payload's ``agent_id`` alone would find no marker and
    strand the subagent's edits (design §3b RISK-2). Reading
    ``agent_id`` first and ``session_id`` second is *this caller's own*
    identity ladder, never a sibling's — the fallback closes RISK-2
    without weakening om9 (DES-054 "Amendment: two-key fallback").
    ``None`` (the shared bucket) is included as the terminal so
    headless/CI/SDK writes remain visible.
    """
    candidates: list[str | None] = []
    for key in ("agent_id", "session_id"):
        value = data.get(key)
        if isinstance(value, str) and value:
            candidates.append(value)
    if None not in candidates:
        candidates.append(None)
    return tuple(candidates)


def _has_any_plan_marker(root: str, identities: tuple[str | None, ...]) -> bool:
    """True when *root* has a plan marker for *any* identity in the list."""
    from biff.markers import has_plan_marker  # noqa: PLC0415

    return any(has_plan_marker(root, identity) for identity in identities)


def handle_pre_tool_use(data: dict[str, object]) -> dict[str, object] | None:
    """Hard-gate Edit/Write on a set plan.

    Returns a ``permissionDecision: "deny"`` response that blocks the
    edit when no plan is set, or ``None`` to allow (exit 0, no output).
    The deny reason instructs the agent how to unblock — set a plan — so
    the gate *drives* the workflow rather than merely whispering a
    reminder.

    This conforms the code to the ``claude-code-biff.tex`` Z model:
    ``PreToolHookAllow`` is enabled for edit tools only when
    ``planSet = ztrue``; every other state yields ``PreToolHookDeny``,
    which blocks the edit (proven exhaustively with ProB — the model is
    the authority the code obeys).

    Identity resolution reads a preference ladder — ``agent_id`` first,
    ``session_id`` second — of *this caller's own* identities: a
    dispatched subagent's ``Bash``-invoked ``biff plan`` writes under the
    leader's ``session_id`` (no channel today for a subagent's ``Bash``
    to learn its own ``agent_id``), so an ``agent_id``-only read would
    permanently deny every subagent edit. Never any-identity-in-worktree
    — a sibling session's marker never satisfies this caller's gate; the
    fallback closes DES-054 RISK-2 without weakening om9's scoping.

    One state allows gracefully rather than deny, because a hard block
    there would strand the agent: no active biff session, where the gate
    has no session state to reason about.
    """
    if not _has_active_session():
        return None

    root = _repo_common_root(data)
    if not _has_any_plan_marker(root, _identity_candidates(data)):
        return _pre_tool_use_deny(
            "Blocked: editing files requires a plan. "
            "Run /plan <what you're working on>, then retry the edit."
        )
    return None


_BEAD_CLAIM_RE = re.compile(r"\bbd\s+update.*--status[=\s]in_progress")
_BEAD_MUTATE_RE = re.compile(r"\bbd\s+(create|update|close|dep)\b")

_LUX_BEADS_REFRESH = (
    "Beads state changed. If lux is showing the beads board, "
    "refresh it now with /lux:beads."
)


def handle_post_bash(data: dict[str, object]) -> str | None:
    """Process PostToolUse Bash — nudge on bead claims and mutations.

    Biff does not depend on beads: the gate is plan-only (DES-051) and
    this handler holds no gate state.  It only emits soft nudges — a
    dotplan reminder on a bead claim, and a lux beads-board refresh when
    lux is showing.

    Returns an ``additionalContext`` string, or ``None`` to stay silent.
    """
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    ti = cast("dict[str, object]", tool_input)
    command = ti.get("command", "")
    if not isinstance(command, str):
        return None

    response = data.get("tool_response", "")
    is_error = data.get("is_error", False)
    is_success = not is_error and isinstance(response, str) and "\u2713" in response

    # Bead claim — nudge Claude to set its dotplan.
    if _BEAD_CLAIM_RE.search(command) and is_success:
        nudge = (
            "You just claimed a bead. Set your dotplan so teammates can see "
            "what you are working on: /plan <bead-id>: <short description>. "
            "Example: /plan biff-dm8: Fix status bar line 2 height"
        )
        lux = _lux_beads_nudge()
        return f"{nudge} {lux}" if lux else nudge

    # Any other bead state change (close, status transition, create, dep) —
    # refresh the lux beads board if it is showing.
    if _BEAD_MUTATE_RE.search(command) and is_success:
        return _lux_beads_nudge()

    return None


def _lux_beads_nudge() -> str | None:
    """Return lux beads board refresh nudge if lux + beads are both active."""
    if _has_beads() and _is_lux_enabled():
        return _LUX_BEADS_REFRESH
    return None


def _parse_pr_event(
    data: dict[str, object],
) -> tuple[str, str, object] | None:
    """Extract (bare_tool, message, pr_number) from a PR tool call.

    Returns ``None`` if the data doesn't represent a valid PR event.
    """
    tool_name = data.get("tool_name", "")
    if not isinstance(tool_name, str):
        return None
    bare = tool_name.rsplit("__", maxsplit=1)[-1] if "__" in tool_name else tool_name
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    ti = cast("dict[str, object]", tool_input)

    if bare == "create_pull_request":
        title = ti.get("title", "")
        if not isinstance(title, str) or not title:
            return None
        response = _parse_tool_response(data.get("tool_response"))
        pr_num = response.get("number")
        if pr_num is None:
            return None
        return bare, f"Created PR #{pr_num}: {title}", pr_num

    if bare == "merge_pull_request":
        pr_num = ti.get("pullNumber") or ti.get("pull_number")
        if not pr_num:
            return None
        title = ti.get("commit_title", "")
        if isinstance(title, str) and title:
            return bare, f"Merged PR #{pr_num}: {title}", pr_num
        return bare, f"Merged PR #{pr_num}", pr_num

    return None


def handle_post_pr(data: dict[str, object]) -> str | None:
    """Process PostToolUse GitHub PR — detect create/merge.

    Returns an ``additionalContext`` string, or ``None`` to stay silent.
    """
    parsed = _parse_pr_event(data)
    if parsed is None:
        return None
    bare, msg, pr_num = parsed

    # Escape message for safe inclusion in a /wall command.
    escaped_msg = json.dumps(msg, ensure_ascii=False)[1:-1]

    # Check if a wall is already active to avoid redundant suggestions.
    from biff.markers import read_wall_marker  # noqa: PLC0415

    wall_active = read_wall_marker(_repo_common_root(data)) is not None

    parts: list[str] = []

    if not wall_active:
        parts.append("This team uses biff for communication.")
        parts.append(f'Consider announcing to the team: /wall "{escaped_msg}" 10m')

    # Lux PR dashboard.
    if _is_lux_enabled() and bare == "create_pull_request":
        parts.append(
            "Lux is active — render a PR dashboard with /lux:dashboard "
            f"showing PR #{pr_num} status, CI checks, and review state."
        )

    return " ".join(parts) if parts else None


def _get_git_branch() -> str:
    """Return the current git branch name, or empty string on failure."""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return ""


_BEAD_BRANCH_RE = re.compile(r"\b[a-z]+-[a-z0-9]{2,4}\b")


def _expand_branch_plan(branch: str) -> str:
    """Build a plan string from a branch name.

    If the branch contains a bead ID (e.g. ``biff-ka4``), resolve
    the title.  Otherwise return the branch name as-is, prefixed
    with ``→`` to indicate automatic provenance.
    """
    from biff._stdlib import expand_bead_id  # noqa: PLC0415

    m = _BEAD_BRANCH_RE.search(branch)
    if m:
        expanded = expand_bead_id(m.group())
        return f"→ {expanded}"
    return f"→ {branch}"


def _repo_common_root(data: dict[str, object] | None = None) -> str:
    """Resolve this repo's common root for the current hook invocation.

    Delegates to :func:`biff._stdlib.get_repo_common_root` — the parent
    of ``git rev-parse --git-common-dir``, which resolves to the same
    absolute path from the main checkout and every linked worktree,
    unlike ``git rev-parse --show-toplevel``'s nearest-worktree view.

    Prefers the hook's own delivered ``cwd`` (``data["cwd"]``) over the
    ambient process cwd when *data* is given: a dispatched subagent's
    hook subprocess does not always inherit its own assigned worktree as
    its ambient cwd, even when its own shell commands do.  With no *data*
    (a git hook, which never receives a Claude Code payload), resolves
    against the process's own cwd, which for a git hook subprocess is
    always correct.
    """
    from biff._stdlib import get_repo_common_root  # noqa: PLC0415

    cwd = data.get("cwd") if data else None
    return get_repo_common_root(cwd if isinstance(cwd, str) and cwd else None)


def _hint_dir(data: dict[str, object] | None = None) -> pathlib.Path:
    """Repo-scoped hint directory: ``~/.punt-labs/biff/hints/{hash}/``.

    Every linked worktree of a repo shares one hint directory (keyed on
    the repo-common-root, not the nearest worktree toplevel) — a repo and
    its worktrees are one coordination unit, not isolated islands.
    """
    from biff.markers import hint_dir as _markers_hint_dir  # noqa: PLC0415

    return _markers_hint_dir(_repo_common_root(data))


def _plan_hint_path(data: dict[str, object] | None = None) -> pathlib.Path:
    """Repo-scoped plan hint file path."""
    return _hint_dir(data) / "plan-hint"


def handle_post_checkout(branch_flag: str) -> str | None:
    """Process git post-checkout — write plan hint for branch switches.

    Writes ``~/.punt-labs/biff/plan-hint`` with the expanded branch plan.
    The PostToolUse Bash handler picks up the hint on the next
    tool call and nudges Claude to set the plan.

    Returns the plan hint text, or ``None`` for file checkouts.
    """
    if branch_flag != "1":
        return None  # File checkout, not branch switch

    branch = _get_git_branch()
    if not branch:
        return None

    hint = "" if branch in ("main", "master") else _expand_branch_plan(branch)

    hint_path = _plan_hint_path()
    hint_path.parent.mkdir(parents=True, exist_ok=True)
    hint_path.write_text(f"{hint}\n")
    return hint or None


def check_plan_hint(data: dict[str, object] | None = None) -> str | None:
    """Check for a plan hint written by a git hook.

    Reads and deletes ``~/.punt-labs/biff/plan-hint``.  Returns an
    ``additionalContext`` string, or ``None`` if no hint exists.
    """
    hint_path = _plan_hint_path(data)
    if not hint_path.exists():
        return None
    try:
        content = hint_path.read_text().strip()
        hint_path.unlink(missing_ok=True)
    except OSError:
        return None

    if not content:
        return (
            "You switched to the default branch. "
            'Clear your plan: /plan with message="" and source="auto".'
        )
    safe = json.dumps(content, ensure_ascii=False)[1:-1]  # escape " and \
    return (
        "Your branch changed. Set your plan: "
        f'/plan with message="{safe}" and source="auto".'
    )


def _get_commit_subject() -> str:
    """Return the most recent commit's subject line, or empty on failure."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%s"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return ""


def handle_post_commit() -> str | None:
    """Process git post-commit — write plan hint with commit subject.

    Writes ``~/.punt-labs/biff/plan-hint`` with ``✓ <subject>``.  The PostToolUse
    Bash handler picks up the hint and nudges Claude to set the plan.

    Returns the plan hint text, or ``None`` if no subject found.
    """
    subject = _get_commit_subject()
    if not subject:
        return None

    hint = f"✓ {subject}"
    hint_path = _plan_hint_path()
    hint_path.parent.mkdir(parents=True, exist_ok=True)
    hint_path.write_text(f"{hint}\n")
    return hint


def _wall_hint_path(data: dict[str, object] | None = None) -> pathlib.Path:
    """Repo-scoped wall hint file path."""
    return _hint_dir(data) / "wall-hint"


def _read_pre_push_refs() -> list[str]:
    """Read pre-push ref lines from stdin (git provides these)."""
    try:
        raw = sys.stdin.read()
        return raw.strip().splitlines() if raw.strip() else []
    except OSError:
        return []


def handle_pre_push(ref_lines: list[str]) -> str | None:
    """Process git pre-push — suggest /wall for default branch pushes.

    Writes ``~/.punt-labs/biff/wall-hint`` when pushing to main/master.
    The PostToolUse Bash handler picks up the hint.

    Returns the wall hint text, or ``None`` for feature branch pushes.
    """
    for line in ref_lines:
        parts = line.split()
        if len(parts) >= 3:
            remote_ref = parts[2]
            if remote_ref in ("refs/heads/main", "refs/heads/master"):
                hint_path = _wall_hint_path()
                hint_path.parent.mkdir(parents=True, exist_ok=True)
                hint_path.write_text("Pushed to default branch\n")
                return "Pushed to default branch"
    return None


def check_wall_hint(data: dict[str, object] | None = None) -> str | None:
    """Check for a wall hint written by a git hook.

    Reads and deletes ``~/.punt-labs/biff/wall-hint``.  Returns an
    ``additionalContext`` string, or ``None`` if no hint exists.
    """
    hint_path = _wall_hint_path(data)
    if not hint_path.exists():
        return None
    try:
        hint_path.unlink(missing_ok=True)
    except OSError:
        return None
    return (
        "You just pushed to the default branch. "
        "Consider announcing to the team: "
        '/wall "<summary of what shipped>" 10m'
    )


def _detect_collisions(data: dict[str, object] | None = None) -> list[str]:
    """Find other active sessions in the same repo (main checkout or any worktree).

    Reads ``~/.punt-labs/biff/active/`` files and returns session keys whose
    repo_name matches AND whose worktree_root matches (or is absent,
    which conservatively counts as a collision).  ``worktree_root``
    comparison is a plain string match — the *current* side is now the
    repo-common-root, so two sessions in *different* linked worktrees of
    the same repo are treated as one coordination unit, matching
    ``_hint_dir()``'s scope.  A stored row from before this change (which
    recorded the nearest worktree toplevel, not the common root) legitimately
    stops matching for a worktree session specifically — a known, narrower
    residual gap tracked alongside the wall-marker one in DESIGN.md.

    Returns an empty list when there is no git root or no active dir.
    """
    from biff._stdlib import (  # noqa: PLC0415
        active_dir,
        find_git_root,
        get_repo_slug,
        sanitize_repo_name,
    )

    repo_root = find_git_root()
    if repo_root is None:
        return []
    current_repo = sanitize_repo_name(get_repo_slug(repo_root) or repo_root.name)
    current_worktree = _repo_common_root(data)

    adir = active_dir()
    if not adir.is_dir():
        return []

    collisions: list[str] = []
    try:
        for f in adir.iterdir():
            if not f.is_file():
                continue
            try:
                lines = f.read_text().strip().splitlines()
                if len(lines) < 2:
                    continue
                session_key, repo_name = lines[0], lines[1]
            except OSError:
                continue

            if repo_name != current_repo:
                continue

            # Third line is worktree_root (optional — old format lacks it).
            file_worktree = lines[2] if len(lines) >= 3 else ""

            # Conservative: if either side has no worktree info, assume collision.
            if file_worktree and current_worktree and file_worktree != current_worktree:
                continue

            collisions.append(session_key)
    except OSError:
        return []
    return collisions


def _capture_session_hint(data: dict[str, object]) -> None:
    """Persist the Claude ``session_id`` for the MCP server.

    SessionStart is the only hook that sees ``session_id`` (on stdin); the
    server reads the hint back from the same ``CLAUDE_PID``-keyed file this
    hook writes to (DES-058), falling back to the process-tree walk only
    when the env var is absent (DES-011).  A missing/empty id is a no-op —
    the server then routes on a fresh hex fallback.
    """
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return
    source = data.get("source")
    from biff.session_id import SessionHint  # noqa: PLC0415

    hint = SessionHint.capture(
        session_id, source if isinstance(source, str) else "startup"
    )
    # Best-effort: a hint-write failure forfeits resume-reclaim (the server
    # falls back to a fresh hex) but must never break session startup.  Log
    # so a broken resume is observable, then swallow.
    try:
        hint.write()
    except OSError:
        logger.warning(
            "Failed to persist session hint; resume-reclaim disabled this session",
            exc_info=True,
        )


def handle_session_start(data: dict[str, object] | None = None) -> str:
    """Build SessionStart(startup) additionalContext.

    Always returns context — at minimum, a /tty nudge.
    Reads the git branch and suggests /plan with auto source.
    Clears *this session's own* stale plan marker so the PreToolUse gate
    starts fresh for it — a new session inherits no plan (Z
    ``StartSession``: ``planSet'(identity) = zfalse``, scoped to the
    starting session's own key only).  A concurrent sibling session's
    marker, keyed under its own identity, is untouched — the direct fix
    for om9's core mechanism ("one session's start wipes every session's
    marker").
    """
    from biff.markers import clear_plan_marker, read_wall_marker  # noqa: PLC0415

    data = data or {}
    worktree = _repo_common_root(data)
    identity = _resolve_identity(data)
    clear_plan_marker(worktree, identity)

    parts: list[str] = [
        "Biff session starting.",
        "Call /tty to name this session (auto-assigns ttyN).",
    ]

    branch = _get_git_branch()
    if branch:
        plan_text = _expand_branch_plan(branch)
        safe = json.dumps(plan_text, ensure_ascii=False)[1:-1]  # escape " and \
        parts.append(
            f"Set your plan from the current branch: "
            f'/plan with message="{safe}" and source="auto".'
        )
    else:
        parts.append(
            "Set your plan with /plan to show teammates what you're working on."
        )

    parts.append("Check /read for unread messages.")

    # Load active wall broadcast.
    wall_text = read_wall_marker(worktree)
    if wall_text:
        parts.append(f"Active wall: {wall_text}")

    collisions = _detect_collisions()
    if collisions:
        keys = ", ".join(collisions)
        n = len(collisions)
        parts.append(
            f"\u26a0 {n} other session(s) active in this worktree ({keys}). "
            "Run /who to check what others are working on before claiming work. "
            "Set /plan before beginning to avoid duplicate effort. "
            "Consider /write to negotiate file ownership, "
            "or use a git worktree for isolation."
        )

    return " ".join(parts)


def handle_session_resume() -> str:
    """Build SessionStart(resume|compact) additionalContext.

    Re-orients Claude after context compaction or resume.
    """
    return "Biff session resumed. Check /read for unread messages."


def handle_pre_compact(data: dict[str, object] | None = None) -> str:
    """Build PreCompact additionalContext.

    Injects the current plan into additionalContext so the model
    retains awareness of what it was working on after compaction.
    """
    from biff.markers import read_plan_marker  # noqa: PLC0415

    data = data or {}
    root = _repo_common_root(data)
    if root:
        plan_text = read_plan_marker(root, _resolve_identity(data))
        if plan_text:
            return f"Current biff plan: {plan_text}. Check /read for unread messages."
    return "Biff session resumed after compaction. Check /read for unread messages."


def handle_session_end() -> int:
    """Convert active-session markers to sentinels for cleanup.

    Only processes sessions belonging to the **current repo** — other
    repos' sessions are left untouched.  This prevents ending one
    Claude Code session from reaping sessions in unrelated repos.

    Returns the number of sessions cleaned up.
    """
    from biff._stdlib import (  # noqa: PLC0415
        active_dir,
        find_git_root,
        get_repo_slug,
        remove_active_session,
        sanitize_repo_name,
        sentinel_dir,
    )

    repo_root = find_git_root()
    if repo_root is None:
        return 0
    current_repo = sanitize_repo_name(get_repo_slug(repo_root) or repo_root.name)

    adir = active_dir()
    if not adir.exists():
        return 0

    count = 0
    for f in adir.iterdir():
        if not f.is_file():
            continue
        try:
            lines = f.read_text().strip().splitlines()
            if len(lines) < 2:
                continue
            session_key, repo_name = lines[0], lines[1]
        except OSError:
            continue

        # Only clean up sessions for THIS repo.
        if repo_name != current_repo:
            continue

        # Write sentinel so the reaper deletes the KV entry.
        sdir = sentinel_dir(repo_name)
        sdir.mkdir(parents=True, exist_ok=True)
        safe = session_key.replace(":", "-")
        try:
            (sdir / safe).write_text(session_key)
        except OSError:
            continue

        # Remove the active marker.
        with suppress(OSError):
            remove_active_session(session_key)
        count += 1
    return count


# ── Claude Code commands ─────────────────────────────────────────────


@_cc_app.command("pre-tool-use")
def cc_pre_tool_use() -> None:
    """PreToolUse Edit|Write — gate on a set plan.

    Fails *closed*: if the gate cannot evaluate its condition (an
    unexpected error reading the plan marker), it denies rather than lets
    the edit through.  A hard control that silently grants access on
    error is the same bug as no control at all (DES-051).
    """
    if not _is_biff_enabled():
        return
    data = _read_hook_input()
    try:
        result = handle_pre_tool_use(data)
    except Exception:  # noqa: BLE001 — hook boundary (PY-EH-6): a gate that cannot evaluate must fail closed
        logger.warning("Plan gate evaluation failed; denying", exc_info=True)
        result = _pre_tool_use_deny(
            "Blocked: could not verify plan state. "
            "Run /plan <what you're working on>, then retry the edit."
        )
    if result is not None:
        _emit(result)


@_cc_app.command("post-bash")
def cc_post_bash() -> None:
    """PostToolUse Bash — bead claims and git checkout nudges."""
    if not _is_biff_enabled():
        return
    data = _read_hook_input()
    result = handle_post_bash(data) or check_plan_hint(data) or check_wall_hint(data)
    if result is not None:
        _emit(_post_tool_use_context(result))


@_cc_app.command("post-pr")
def cc_post_pr() -> None:
    """PostToolUse GitHub PR — suggest /wall for create/merge."""
    if not _is_biff_enabled():
        return
    data = _read_hook_input()
    result = handle_post_pr(data)
    if result is not None:
        _emit(_post_tool_use_context(result))


@_cc_app.command("session-start")
def cc_session_start() -> None:
    """SessionStart(startup) — auto-tty, plan from branch, check unread."""
    if not _is_biff_enabled():
        return
    data = _read_hook_input()
    _capture_session_hint(data)
    result = handle_session_start(data)
    _emit(_hook_context("SessionStart", result))


@_cc_app.command("session-resume")
def cc_session_resume() -> None:
    """SessionStart(resume/compact) — re-orient after context loss."""
    if not _is_biff_enabled():
        return
    _capture_session_hint(_read_hook_input())
    result = handle_session_resume()
    _emit(_hook_context("SessionStart", result))


@_cc_app.command("session-end")
def cc_session_end() -> None:
    """SessionEnd — convert active sessions to sentinels for cleanup."""
    if not _is_biff_enabled():
        return
    handle_session_end()


@_cc_app.command("pre-compact")
def cc_pre_compact() -> None:
    """PreCompact — inject plan into additionalContext before compaction."""
    if not _is_biff_enabled():
        return
    result = handle_pre_compact(_read_hook_input())
    _emit(_hook_context("PreCompact", result))


# ── Git commands ─────────────────────────────────────────────────────


@_git_app.command("post-checkout")
def git_post_checkout(
    prev_head: str = typer.Argument("", help="Previous HEAD ref"),  # noqa: ARG001
    new_head: str = typer.Argument("", help="New HEAD ref"),  # noqa: ARG001
    branch_flag: str = typer.Argument("", help="1=branch checkout, 0=file"),
) -> None:
    """post-checkout — write plan hint from branch name."""
    if not _is_biff_enabled():
        return
    handle_post_checkout(branch_flag)


@_git_app.command("post-commit")
def git_post_commit() -> None:
    """post-commit — write plan hint with commit subject."""
    if not _is_biff_enabled():
        return
    handle_post_commit()


@_git_app.command("pre-push")
def git_pre_push(
    remote: str = typer.Argument("", help="Remote name"),  # noqa: ARG001
) -> None:
    """pre-push — suggest /wall for default branch pushes."""
    if not _is_biff_enabled():
        return
    ref_lines = _read_pre_push_refs()
    handle_pre_push(ref_lines)
