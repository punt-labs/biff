"""Hook dispatcher for biff lifecycle events (DES-017).

All hook shell scripts delegate to ``biff hook <layer> <event>``.
Business logic lives here in versioned Python; shell scripts are
thin dispatchers with only a fast ``.biff`` file-existence gate.

Layer 1: Claude Code hooks — capture agent lifecycle events.
Layer 2: Git hooks — capture code lifecycle events.
"""

from __future__ import annotations

import json
import re
import sys
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


def _post_tool_use_context(context: str) -> dict[str, object]:
    """Build PostToolUse hook output with ``additionalContext`` only."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": context,
        }
    }


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


# ── Claude Code commands ─────────────────────────────────────────────


@_cc_app.command("post-bash")
def cc_post_bash() -> None:
    """PostToolUse Bash — bead claims and git checkout nudges."""
    if not _is_biff_enabled():
        return
    data = _read_hook_input()
    result = handle_post_bash(data)
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
    """SessionStart — auto-tty, plan from branch, check unread.

    Stub: full implementation in biff-6we.
    """


@_cc_app.command("session-resume")
def cc_session_resume() -> None:
    """SessionStart (resume/compact) — refresh presence, re-announce plan.

    Stub: full implementation in biff-6we.
    """


@_cc_app.command("session-end")
def cc_session_end() -> None:
    """SessionEnd — immediate session cleanup.

    Stub: full implementation in biff-w5c.
    """


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
    prev_head: str = typer.Argument("", help="Previous HEAD ref"),
    new_head: str = typer.Argument("", help="New HEAD ref"),
    branch_flag: str = typer.Argument("", help="1=branch checkout, 0=file"),
) -> None:
    """post-checkout — update plan from branch name.

    Stub: full implementation in biff-ka4.
    """


@_git_app.command("post-commit")
def git_post_commit() -> None:
    """post-commit — update plan with commit subject.

    Stub: full implementation in biff-crz.
    """


@_git_app.command("pre-push")
def git_pre_push(
    remote: str = typer.Argument("", help="Remote name"),
) -> None:
    """pre-push — suggest /wall for default branch pushes.

    Stub: full implementation in biff-9e7.
    """
