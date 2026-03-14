"""Hook dispatcher for biff lifecycle events (DES-017).

All hook shell scripts delegate to ``biff hook <layer> <event>``.
Business logic lives here in versioned Python; shell scripts are
thin dispatchers with only a fast ``.biff`` file-existence gate.

Layer 1: Claude Code hooks — capture agent lifecycle events.
Layer 2: Git hooks — capture code lifecycle events.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import select
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
    from biff._stdlib import find_git_root, is_enabled  # noqa: PLC0415

    repo_root = find_git_root()
    return repo_root is not None and is_enabled(repo_root)


def _has_beads() -> bool:
    """Check whether beads is available (``.beads/`` exists in git root)."""
    from biff._stdlib import find_git_root  # noqa: PLC0415

    repo_root = find_git_root()
    return repo_root is not None and (repo_root / ".beads").is_dir()


def _is_lux_enabled() -> bool:
    """Check whether lux display mode is enabled.

    Reads ``.lux/config.md`` YAML frontmatter for ``display: "y"``.
    Returns ``False`` if the file is absent, malformed, or display is off.
    """
    from biff._stdlib import find_git_root  # noqa: PLC0415

    repo_root = find_git_root()
    if repo_root is None:
        return False
    config = repo_root / ".lux" / "config.md"
    if not config.is_file():
        return False
    try:
        text = config.read_text()
        # Parse YAML frontmatter: ---\ndisplay: "y"\n---
        if not text.startswith("---"):
            return False
        end = text.find("---", 3)
        if end == -1:
            return False
        frontmatter = text[3:end]
        for line in frontmatter.splitlines():
            stripped = line.strip()
            if stripped.startswith("display:"):
                value = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                return value == "y"
    except OSError:
        pass
    return False


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


def _pre_tool_use_ask(reason: str) -> dict[str, object]:
    """Build PreToolUse hook output that warns but does not block.

    Uses ``ask`` instead of ``deny`` so agents can proceed after
    setting their plan rather than halting entirely (biff-nxtb).
    """
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }


def handle_pre_tool_use(data: dict[str, object]) -> dict[str, object] | None:  # noqa: ARG001
    """Gate Edit/Write on plan-set AND bead-claimed.

    Returns a deny response if either condition is missing, or ``None``
    to allow (exit 0, no output).

    The Z spec proves no path reaches file editing without both
    conditions met (161K states, 789K transitions verified).
    """
    from biff.markers import check_bead_in_progress, has_plan_marker  # noqa: PLC0415

    worktree = _get_worktree_root()
    plan_set = has_plan_marker(worktree)
    bead_status = check_bead_in_progress(worktree)

    if not plan_set and bead_status != "yes":
        if bead_status == "unavailable":
            return _pre_tool_use_ask(
                "Set your plan before editing files. "
                "Run: /plan <what you're working on>. "
                "Bead status could not be checked (bd unavailable)."
            )
        return _pre_tool_use_ask(
            "Set your plan and claim a bead before editing files. "
            "Run: /plan <what you're working on>, "
            "then: bd update <bead-id> --status=in_progress"
        )
    if not plan_set:
        return _pre_tool_use_ask(
            "Set your plan before editing files. Run: /plan <what you're working on>"
        )
    if bead_status == "unavailable":
        # Plan is set but bd is unavailable — allow gracefully.
        return None
    if bead_status == "no":
        return _pre_tool_use_ask(
            "Claim a bead before editing files. "
            "Run: bd update <bead-id> --status=in_progress"
        )
    return None


_BEAD_CLAIM_RE = re.compile(r"\bbd\s+update.*--status[=\s]in_progress")
_BEAD_CLOSE_RE = re.compile(r"\bbd\s+close\b")
_BEAD_STATUS_CHANGE_RE = re.compile(
    r"\bbd\s+update.*--status[=\s](?!in_progress)\w+",
)
_BEAD_MUTATE_RE = re.compile(r"\bbd\s+(create|update|close|dep)\b")

_LUX_BEADS_REFRESH = (
    "Beads state changed. If lux is showing the beads board, "
    "refresh it now with /lux:beads."
)


def handle_post_bash(data: dict[str, object]) -> str | None:
    """Process PostToolUse Bash — detect bead claims, closes, and mutations.

    Manages the bead-active marker file for the PreToolUse cache:
    - On successful ``bd update --status=in_progress``: write marker.
    - On successful ``bd close``: clear marker (forces re-check on next gate).

    When lux is enabled and beads state changes, nudges Claude to
    refresh the beads board (biff-og4p consumer integration).

    Returns an ``additionalContext`` string, or ``None`` to stay silent.
    """
    from biff.markers import clear_bead_marker, write_bead_marker  # noqa: PLC0415

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

    worktree = _get_worktree_root()

    # Bead close — clear marker so next PreToolUse re-checks via subprocess.
    if _BEAD_CLOSE_RE.search(command) and is_success:
        if worktree:
            clear_bead_marker(worktree)
        return _lux_beads_nudge()

    # Bead claim — write marker for fast PreToolUse gate.
    if _BEAD_CLAIM_RE.search(command) and is_success:
        if worktree:
            write_bead_marker(worktree)
        nudge = (
            "You just claimed a bead. Set your dotplan so teammates can see "
            "what you are working on: /plan <bead-id>: <short description>. "
            "Example: /plan biff-dm8: Fix status bar line 2 height"
        )
        lux = _lux_beads_nudge()
        return f"{nudge} {lux}" if lux else nudge

    # Status transition away from in_progress — clear marker.
    if _BEAD_STATUS_CHANGE_RE.search(command) and is_success:
        if worktree:
            clear_bead_marker(worktree)
        return _lux_beads_nudge()

    # Other bead mutations (create, dep add, generic update).
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

    safe_msg = json.dumps(msg, ensure_ascii=False)[1:-1]

    # Check if a wall is already active to avoid redundant suggestions.
    from biff.markers import read_wall_marker  # noqa: PLC0415

    wall_active = read_wall_marker(_get_worktree_root()) is not None

    parts: list[str] = ["This team uses biff for communication."]
    if wall_active:
        parts.append(f'Notify the relevant human directly: /write @human "{safe_msg}"')
    else:
        parts.append(f"Consider announcing to the team: /wall {msg}")
        parts.append(
            f'Also notify the relevant human directly: /write @human "{safe_msg}"'
        )

    # Lux PR dashboard (biff-g75a consumer integration).
    if _is_lux_enabled() and bare == "create_pull_request":
        parts.append(
            "Lux is active — render a PR dashboard with /lux:dashboard "
            f"showing PR #{pr_num} status, CI checks, and review state."
        )

    return " ".join(parts)


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
    from biff._stdlib import expand_bead_id  # noqa: PLC0415

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
    from biff.markers import hint_dir as _markers_hint_dir  # noqa: PLC0415

    return _markers_hint_dir(_get_worktree_root())


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


def _detect_collisions() -> list[str]:
    """Find other active sessions in the same worktree.

    Reads ``~/.biff/active/`` files and returns session keys whose
    repo_name matches AND whose worktree_root matches (or is absent,
    which conservatively counts as a collision).

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
    current_worktree = _get_worktree_root()

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


def handle_session_start() -> str:
    """Build SessionStart(startup) additionalContext.

    Always returns context — at minimum, a /tty nudge.
    Reads the git branch and suggests /plan with auto source.
    Clears stale plan marker so the PreToolUse gate starts fresh.
    """
    from biff.markers import clear_plan_marker, read_wall_marker  # noqa: PLC0415

    worktree = _get_worktree_root()
    clear_plan_marker(worktree)

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

    # Load active wall broadcast (biff-41j).
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
            "Consider /write @other to negotiate file ownership, "
            "or use a git worktree for isolation."
        )

    return " ".join(parts)


def handle_session_resume() -> str:
    """Build SessionStart(resume|compact) additionalContext.

    Re-orients Claude after context compaction or resume.
    """
    return "Biff session resumed. Check /read for unread messages."


def handle_stop() -> str | None:
    """Check for unread messages at session stop.

    Reads the per-session unread file maintained by the MCP server.
    Returns an ``additionalContext`` reminder, or ``None`` if no
    unread messages.  This is a soft gate — always exit 0.
    """
    from biff.session_key import find_session_key  # noqa: PLC0415

    unread_path = (
        pathlib.Path.home() / ".biff" / "unread" / f"{find_session_key()}.json"
    )
    if not unread_path.is_file():
        return None
    try:
        raw: object = cast("object", json.loads(unread_path.read_text()))
        if not isinstance(raw, dict):
            return None
        data = cast("dict[str, object]", raw)
        count = data.get("count", 0)
        if not isinstance(count, int) or count <= 0:
            return None
        plural = "s" if count != 1 else ""
        return f"You have {count} unread message{plural}. Run /read before finishing."
    except (json.JSONDecodeError, OSError):
        return None


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
    """PreToolUse Edit|Write — gate on plan-set AND bead-claimed."""
    if not _is_biff_enabled():
        return
    data = _read_hook_input()
    result = handle_pre_tool_use(data)
    if result is not None:
        _emit(result)


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
    result = handle_session_start()
    _emit(_hook_context("SessionStart", result))


@_cc_app.command("session-resume")
def cc_session_resume() -> None:
    """SessionStart(resume/compact) — re-orient after context loss."""
    if not _is_biff_enabled():
        return
    result = handle_session_resume()
    _emit(_hook_context("SessionStart", result))


@_cc_app.command("session-end")
def cc_session_end() -> None:
    """SessionEnd — convert active sessions to sentinels for cleanup."""
    if not _is_biff_enabled():
        return
    handle_session_end()


@_cc_app.command("stop")
def cc_stop() -> None:
    """Stop — unread message reminder (soft gate, never blocks)."""
    if not _is_biff_enabled():
        return
    result = handle_stop()
    if result is not None:
        _emit({"decision": "block", "reason": result})


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
