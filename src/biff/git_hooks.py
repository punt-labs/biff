"""Git hook deployment for biff (DES-017).

Deploys thin dispatcher lines into ``.git/hooks/`` files.
Coexists with existing hooks (e.g. beads post-merge) by
appending/removing a marked block rather than overwriting.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from biff.config import find_git_root

logger = logging.getLogger(__name__)

# Marker comments bracket the biff dispatch line so we can
# identify and remove our additions without touching other hooks.
_MARKER_START = "# >>> biff hook dispatcher (DES-017)"
_MARKER_END = "# <<< biff hook dispatcher"


def resolve_hooks_dir(repo_root: Path) -> Path | None:
    """Resolve the git hooks directory for *repo_root*.

    ``<root>/.git/hooks`` is wrong for two common layouts: in a linked
    worktree ``.git`` is a *file* (so that dir does not exist and hooks
    live under the main repository's common git dir), and ``core.hooksPath``
    can relocate hooks anywhere.  ``git rev-parse --git-path hooks`` is the
    one lookup that honors worktree/submodule redirection AND
    ``core.hooksPath`` -- asking git avoids reimplementing its rules.

    Git prints an absolute path for worktrees and an absolute
    ``core.hooksPath``; otherwise it prints a path relative to *repo_root*
    (``.git/hooks`` or a relative ``core.hooksPath``), which we anchor to
    *repo_root*.  Returns ``None`` when *repo_root* is not a git repository
    or ``git`` is not on ``PATH``.
    """
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(repo_root), "rev-parse", "--git-path", "hooks"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None

    hooks = Path(result.stdout.strip())
    return hooks if hooks.is_absolute() else repo_root / hooks


# Map of hook name → dispatch command.
# Each entry becomes a block appended to .git/hooks/<name>.
GIT_HOOKS: dict[str, str] = {
    "post-checkout": 'biff hook git post-checkout "$1" "$2" "$3" 2>/dev/null || true',
    "post-commit": "biff hook git post-commit 2>/dev/null || true",
    "pre-push": 'biff hook git pre-push "$1" 2>/dev/null || true',
}


def _biff_block(command: str) -> str:
    """Build the marked block for a biff dispatch line."""
    return f"{_MARKER_START}\n{command}\n{_MARKER_END}\n"


def _has_biff_block(content: str) -> bool:
    """Check if a hook file already contains a biff block."""
    return _MARKER_START in content


def _remove_biff_block(content: str) -> str:
    """Remove the biff block from hook file content."""
    lines = content.splitlines(keepends=True)
    result: list[str] = []
    in_block = False
    for line in lines:
        if _MARKER_START in line:
            in_block = True
            continue
        if in_block and _MARKER_END in line:
            in_block = False
            continue
        if not in_block:
            result.append(line)
    return "".join(result)


def deploy_git_hooks(repo_root: Path | None = None) -> list[str]:
    """Deploy biff dispatch lines into ``.git/hooks/``.

    For each hook in :data:`GIT_HOOKS`:
    - If the hook file doesn't exist, creates it with a shebang + biff block.
    - If the file exists but has no biff block, appends the block.
    - If the file already has a biff block, replaces it (idempotent).

    Returns a list of hook names that were created or updated.
    """
    root = repo_root or find_git_root()
    if root is None:
        return []

    hooks_dir = resolve_hooks_dir(root)
    if hooks_dir is None:
        # Never a silent skip: surface that git could not resolve a hooks
        # directory (not a git repo, or git missing) so the install path can
        # tell the user rather than deploying nothing without a word.
        logger.warning("no git hooks directory resolved for %s; deployed nothing", root)
        return []
    hooks_dir.mkdir(parents=True, exist_ok=True)

    updated: list[str] = []
    for name, command in GIT_HOOKS.items():
        hook_path = hooks_dir / name
        block = _biff_block(command)

        if hook_path.exists():
            content = hook_path.read_text()
            if _has_biff_block(content):
                # Replace existing block (idempotent update).
                new_content = _remove_biff_block(content) + block
                if new_content != content:
                    hook_path.write_text(new_content)
                    updated.append(name)
            else:
                # Append to existing hook (coexistence).
                hook_path.write_text(content.rstrip("\n") + "\n\n" + block)
                updated.append(name)
            # Ensure executable even for existing files.
            if not hook_path.stat().st_mode & 0o111:
                hook_path.chmod(hook_path.stat().st_mode | 0o755)
        else:
            # Create new hook file.
            hook_path.write_text(f"#!/usr/bin/env bash\n{block}")
            hook_path.chmod(0o755)
            updated.append(name)

    return updated


def remove_git_hooks(repo_root: Path | None = None) -> list[str]:
    """Remove biff dispatch lines from ``.git/hooks/``.

    For each hook in :data:`GIT_HOOKS`:
    - If the file has a biff block, removes it.
    - If the file becomes empty (only shebang + whitespace), deletes it.
    - If the file has other content, leaves it intact.

    Returns a list of hook names that were cleaned up.
    """
    root = repo_root or find_git_root()
    if root is None:
        return []

    hooks_dir = resolve_hooks_dir(root)
    if hooks_dir is None or not hooks_dir.is_dir():
        return []

    removed: list[str] = []
    for name in GIT_HOOKS:
        hook_path = hooks_dir / name
        if not hook_path.exists():
            continue

        content = hook_path.read_text()
        if not _has_biff_block(content):
            continue

        cleaned = _remove_biff_block(content)
        # If only shebang + whitespace remains, delete the file.
        stripped = cleaned.strip()
        if not stripped or stripped == "#!/usr/bin/env bash" or stripped == "#!/bin/sh":
            hook_path.unlink()
        else:
            hook_path.write_text(cleaned)
        removed.append(name)

    return removed


def check_git_hooks(repo_root: Path | None = None) -> list[str]:
    """Check which biff git hooks are missing.

    Returns a list of hook names that should be installed but aren't.
    """
    root = repo_root or find_git_root()
    if root is None:
        return list(GIT_HOOKS)

    hooks_dir = resolve_hooks_dir(root)
    if hooks_dir is None or not hooks_dir.is_dir():
        return list(GIT_HOOKS)

    missing: list[str] = []
    for name in GIT_HOOKS:
        hook_path = hooks_dir / name
        if not hook_path.exists() or not _has_biff_block(hook_path.read_text()):
            missing.append(name)

    return missing
