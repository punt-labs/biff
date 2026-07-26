"""Git hook deployment for biff (DES-017).

Deploys thin dispatcher lines into ``.git/hooks/`` files.
Coexists with existing hooks (e.g. beads post-merge) by
appending/removing a marked block rather than overwriting.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from biff._stdlib import ensure_real_dir, is_regular_file
from biff.config import find_git_root

logger = logging.getLogger(__name__)

# One notice, emitted verbatim by every caller that cannot resolve a hooks
# directory (``biff install`` and both ``enable`` surfaces), so the failure
# never manifests as a silent "success with zero hooks".
HOOKS_DIR_UNRESOLVED_NOTICE = (
    "NOTICE: could not resolve a git hooks directory "
    "(not a git repository, or git is not on PATH); no git hooks were deployed."
)

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
# ``2>/dev/null || true`` is a deliberate total suppression: a git hook must
# never break the developer's git command. If ``biff hook`` is missing, errors,
# or the repo is not biff-enabled, the dispatcher stays silent and exits 0 so
# the commit/checkout/push proceeds unimpeded (biff gates on the marker inside).
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
    # Parent-dir symlink guard (parity with ci_workflow / write_enabled_marker).
    # Only components *inside* the repo can be committed and thus attacker-set,
    # so we police those with ensure_real_dir. A hooks dir OUTSIDE the repo
    # (a linked worktree's common git dir, or an absolute core.hooksPath) is
    # git-managed, not committed, and lives above/beside the repo — trust it and
    # just mkdir, never treating its ancestors (which we do not own) as suspect.
    if hooks_dir == root or root in hooks_dir.parents:
        ensure_real_dir(root, hooks_dir)
    else:
        hooks_dir.mkdir(parents=True, exist_ok=True)

    return [
        name
        for name, command in GIT_HOOKS.items()
        if _deploy_one_hook(hooks_dir / name, _biff_block(command))
    ]


def _ensure_executable(hook_path: Path) -> None:
    """Add the user/group/other execute bits if the file is not executable."""
    if not hook_path.stat().st_mode & 0o111:
        hook_path.chmod(hook_path.stat().st_mode | 0o755)


def _refresh_existing_hook(hook_path: Path, block: str) -> bool:
    """Update a regular hook file in place; return True if its content changed.

    Replaces an existing biff block idempotently, or appends one to a foreign
    hook (coexistence). Always ensures the file stays executable.
    """
    content = hook_path.read_text()
    if _has_biff_block(content):
        new_content = _remove_biff_block(content) + block
        changed = new_content != content
        if changed:
            hook_path.write_text(new_content)
    else:
        hook_path.write_text(content.rstrip("\n") + "\n\n" + block)
        changed = True
    _ensure_executable(hook_path)
    return changed


def _deploy_one_hook(hook_path: Path, block: str) -> bool:
    """Deploy/refresh one hook file; return True if created or updated.

    Regular files only: a symlink at the path is replaced (never followed, so
    its target is never clobbered); a non-regular, non-symlink entry (e.g. a
    directory) is not ours and is left untouched.
    """
    if hook_path.is_symlink():
        hook_path.unlink()
    if is_regular_file(hook_path):
        return _refresh_existing_hook(hook_path, block)
    if hook_path.exists():
        return False  # non-regular entry (e.g. a directory) — not ours, skip
    hook_path.write_text(f"#!/usr/bin/env bash\n{block}")
    hook_path.chmod(0o755)
    return True


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
        # Regular files only: a symlinked hook path is not ours — never follow
        # it to read/rewrite/delete its target; a missing or non-file path has
        # nothing of ours to remove.
        if not is_regular_file(hook_path):
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
        # Regular files only: a symlinked hook path is not ours and must not be
        # followed to read an arbitrary target, so treat it (and any missing or
        # non-file path) as "not deployed".
        if not is_regular_file(hook_path) or not _has_biff_block(hook_path.read_text()):
            missing.append(name)

    return missing
