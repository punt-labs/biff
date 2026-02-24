"""Git hook deployment for biff (DES-017).

Deploys thin dispatcher lines into ``.git/hooks/`` files.
Coexists with existing hooks (e.g. beads post-merge) by
appending/removing a marked block rather than overwriting.
"""

from __future__ import annotations

from pathlib import Path

from biff.config import find_git_root

# Marker comments bracket the biff dispatch line so we can
# identify and remove our additions without touching other hooks.
_MARKER_START = "# >>> biff hook dispatcher (DES-017)"
_MARKER_END = "# <<< biff hook dispatcher"

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

    hooks_dir = root / ".git" / "hooks"
    if not hooks_dir.is_dir():
        return []

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

    hooks_dir = root / ".git" / "hooks"
    if not hooks_dir.is_dir():
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

    hooks_dir = root / ".git" / "hooks"
    if not hooks_dir.is_dir():
        return list(GIT_HOOKS)

    missing: list[str] = []
    for name in GIT_HOOKS:
        hook_path = hooks_dir / name
        if not hook_path.exists() or not _has_biff_block(hook_path.read_text()):
            missing.append(name)

    return missing
