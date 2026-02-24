"""Hook dispatcher for biff lifecycle events (DES-017).

All hook shell scripts delegate to ``biff hook <layer> <event>``.
Business logic lives here in versioned Python; shell scripts are
thin dispatchers with only a fast ``.biff`` file-existence gate.

Layer 1: Claude Code hooks — capture agent lifecycle events.
Layer 2: Git hooks — capture code lifecycle events.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import subprocess
import sys
from contextlib import suppress
from typing import cast

import typer

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
    """Check ``.biff`` + ``.biff.local`` gating (lazy import)."""
    from biff.config import find_git_root, is_enabled  # noqa: PLC0415

    repo_root = find_git_root()
    return repo_root is not None and is_enabled(repo_root)


def _read_hook_input() -> dict[str, object]:
    """Read JSON hook payload from stdin."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        data = json.loads(raw)
        if isinstance(data, dict):
            return cast("dict[str, object]", data)
        return {}
    except (json.JSONDecodeError, OSError):
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

_BEAD_CLAIM_RE = re.compile(r"\bbd\s+update.*--status[=\s]in_progress")


def handle_post_bash(data: dict[str, object]) -> str | None:
    """Process PostToolUse Bash — detect bead claims.

    Returns an ``additionalContext`` string, or ``None`` to stay silent.
    """
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    ti = cast("dict[str, object]", tool_input)
    command = ti.get("command", "")
    if not isinstance(command, str):
        return None

    if not _BEAD_CLAIM_RE.search(command):
        return None

    response = data.get("tool_response", "")
    if not isinstance(response, str) or "\u2713" not in response:
        return None

    return (
        "You just claimed a bead. Set your dotplan so teammates can see "
        "what you are working on: /plan <bead-id>: <short description>. "
        "Example: /plan biff-dm8: Fix status bar line 2 height"
    )


def handle_post_pr(data: dict[str, object]) -> str | None:
    """Process PostToolUse GitHub PR — detect create/merge.

    Returns an ``additionalContext`` string, or ``None`` to stay silent.
    """
    tool_name = data.get("tool_name", "")
    if not isinstance(tool_name, str):
        return None

    # Strip plugin prefix to get bare tool name
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
        msg = f"Created PR #{pr_num}: {title}"

    elif bare == "merge_pull_request":
        pr_num = ti.get("pullNumber") or ti.get("pull_number")
        if not pr_num:
            return None
        title = ti.get("commit_title", "")
        if isinstance(title, str) and title:
            msg = f"Merged PR #{pr_num}: {title}"
        else:
            msg = f"Merged PR #{pr_num}"

    else:
        return None

    return (
        "This team uses biff for communication. "
        f"Consider announcing to the team: /wall {msg}"
    )


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
    except (FileNotFoundError, TimeoutError, OSError):
        pass
    return ""


_BEAD_BRANCH_RE = re.compile(r"\b[a-z]+-[a-z0-9]{2,4}\b")


def _expand_branch_plan(branch: str) -> str:
    """Build a plan string from a branch name.

    If the branch contains a bead ID (e.g. ``biff-ka4``), resolve
    the title.  Otherwise return the branch name as-is, prefixed
    with ``→`` to indicate automatic provenance.
    """
    from biff.server.tools.plan import expand_bead_id  # noqa: PLC0415

    m = _BEAD_BRANCH_RE.search(branch)
    if m:
        expanded = expand_bead_id(m.group())
        return f"→ {expanded}"
    return f"→ {branch}"


def _get_worktree_root() -> str:
    """Return the git worktree root path, or empty string on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, TimeoutError, OSError):
        pass
    return ""


def _hint_dir() -> pathlib.Path:
    """Worktree-scoped hint directory: ``~/.biff/hints/{hash}/``.

    Each git worktree gets its own hint directory so multiple sessions
    in different worktrees don't race on shared hint files.  Sessions
    in the same worktree share a hint directory — the coordination
    contract requires worktree isolation for concurrent sessions.
    """
    root = _get_worktree_root()
    h = hashlib.sha256(root.encode()).hexdigest()[:16] if root else "default"
    return pathlib.Path.home() / ".biff" / "hints" / h


def _plan_hint_path() -> pathlib.Path:
    """Worktree-scoped plan hint file path."""
    return _hint_dir() / "plan-hint"


def handle_post_checkout(branch_flag: str) -> str | None:
    """Process git post-checkout — write plan hint for branch switches.

    Writes ``~/.biff/plan-hint`` with the expanded branch plan.
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


def check_plan_hint() -> str | None:
    """Check for a plan hint written by a git hook.

    Reads and deletes ``~/.biff/plan-hint``.  Returns an
    ``additionalContext`` string, or ``None`` if no hint exists.
    """
    hint_path = _plan_hint_path()
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
    except (FileNotFoundError, TimeoutError, OSError):
        pass
    return ""


def handle_post_commit() -> str | None:
    """Process git post-commit — write plan hint with commit subject.

    Writes ``~/.biff/plan-hint`` with ``✓ <subject>``.  The PostToolUse
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


def _wall_hint_path() -> pathlib.Path:
    """Worktree-scoped wall hint file path."""
    return _hint_dir() / "wall-hint"


def _read_pre_push_refs() -> list[str]:
    """Read pre-push ref lines from stdin (git provides these)."""
    try:
        raw = sys.stdin.read()
        return raw.strip().splitlines() if raw.strip() else []
    except OSError:
        return []


def handle_pre_push(ref_lines: list[str]) -> str | None:
    """Process git pre-push — suggest /wall for default branch pushes.

    Writes ``~/.biff/wall-hint`` when pushing to main/master.
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


def check_wall_hint() -> str | None:
    """Check for a wall hint written by a git hook.

    Reads and deletes ``~/.biff/wall-hint``.  Returns an
    ``additionalContext`` string, or ``None`` if no hint exists.
    """
    hint_path = _wall_hint_path()
    if not hint_path.exists():
        return None
    try:
        hint_path.unlink(missing_ok=True)
    except OSError:
        return None
    return (
        "You just pushed to the default branch. "
        "Consider announcing to the team: /wall <summary of what shipped>"
    )


def handle_session_start(data: dict[str, object]) -> str:  # noqa: ARG001
    """Build SessionStart(startup) additionalContext.

    Always returns context — at minimum, a /tty nudge.
    Reads the git branch and suggests /plan with auto source.
    """
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
    return " ".join(parts)


def handle_session_resume() -> str:
    """Build SessionStart(resume|compact) additionalContext.

    Re-orients Claude after context compaction or resume.
    """
    return "Biff session resumed. Check /read for unread messages."


def handle_session_end() -> int:
    """Convert active-session markers to sentinels for cleanup.

    Only processes sessions belonging to the **current repo** — other
    repos' sessions are left untouched.  This prevents ending one
    Claude Code session from reaping sessions in unrelated repos.

    Returns the number of sessions cleaned up.
    """
    from pathlib import Path  # noqa: PLC0415

    from biff.config import (  # noqa: PLC0415
        find_git_root,
        get_repo_slug,
        sanitize_repo_name,
    )
    from biff.server.app import (  # noqa: PLC0415
        remove_active_session,
        sentinel_dir,
    )

    repo_root = find_git_root()
    if repo_root is None:
        return 0
    current_repo = sanitize_repo_name(get_repo_slug(repo_root) or repo_root.name)

    active_dir = Path.home() / ".biff" / "active"
    if not active_dir.exists():
        return 0

    count = 0
    for f in active_dir.iterdir():
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


@_cc_app.command("post-bash")
def cc_post_bash() -> None:
    """PostToolUse Bash — bead claims and git checkout nudges."""
    if not _is_biff_enabled():
        return
    data = _read_hook_input()
    result = handle_post_bash(data) or check_plan_hint() or check_wall_hint()
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
    result = handle_session_start(data)
    _emit(_hook_context("SessionStart", result))


@_cc_app.command("session-resume")
def cc_session_resume() -> None:
    """SessionStart(resume/compact) — re-orient after context loss."""
    if not _is_biff_enabled():
        return
    _read_hook_input()  # consume stdin even if unused
    result = handle_session_resume()
    _emit(_hook_context("SessionStart", result))


@_cc_app.command("session-end")
def cc_session_end() -> None:
    """SessionEnd — convert active sessions to sentinels for cleanup."""
    if not _is_biff_enabled():
        return
    _read_hook_input()  # consume stdin
    handle_session_end()


@_cc_app.command("stop")
def cc_stop() -> None:
    """Stop — presence heartbeat.

    Stub: full implementation in biff-cs5.
    """


@_cc_app.command("pre-compact")
def cc_pre_compact() -> None:
    """PreCompact — snapshot plan to additionalContext.

    Stub: full implementation in biff-sgl.
    """


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
