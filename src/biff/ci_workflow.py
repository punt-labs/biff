"""CI workflow deployment for biff.

Deploys a standalone ``biff-notify.yml`` workflow that fires on
``workflow_run`` completion and posts a ``biff wall`` on failure.
No existing workflow files are touched.
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path

from biff._stdlib import ensure_real_dir
from biff.config import find_git_root

_WORKFLOW_NAME = "biff-notify.yml"


def _template_content() -> str:
    """Read the bundled workflow template."""
    return (
        importlib.resources.files("biff.data")
        .joinpath(_WORKFLOW_NAME)
        .read_text(encoding="utf-8")
    )


def deploy_ci_workflow(repo_root: Path | None = None) -> bool:
    """Deploy biff-notify.yml to ``.github/workflows/``.

    Returns ``True`` if the file was created or updated, ``False`` if it was
    already current (a no-op).  Raises ``ValueError`` when no usable repo root
    can be determined -- a real failure, distinct from the ``False`` no-op, so
    callers (e.g. ``enable``) can fail safe on it rather than mistaking it for
    "already deployed".
    """
    root = repo_root or find_git_root()
    if root is None or not root.is_dir():
        raise ValueError(
            "cannot deploy the CI workflow: no usable repo root "
            f"(got {root!r}); run inside a git repository."
        )

    workflows_dir = root / ".github" / "workflows"
    # Parent-dir symlink guard: a committed symlinked `.github` (or
    # `.github/workflows`) would let `mkdir -p` + the write escape the repo.
    # ensure_real_dir validates every component below `root` and replaces any
    # symlinked one with a real directory, so the write stays inside the repo.
    ensure_real_dir(root, workflows_dir)

    target = workflows_dir / _WORKFLOW_NAME
    template = _template_content()

    # Symlink guard (mirrors write_enabled_marker): an untrusted checkout could
    # commit biff-notify.yml as a symlink; replace it with a real file rather
    # than following it and clobbering the link's target. Done before the
    # freshness read so a symlink is never left in place as "up to date".
    if target.is_symlink():
        target.unlink()

    if target.is_file() and target.read_text(encoding="utf-8") == template:
        return False  # Already up to date

    target.write_text(template, encoding="utf-8")
    return True


def remove_ci_workflow(repo_root: Path | None = None) -> bool:
    """Remove biff-notify.yml from ``.github/workflows/``.

    Returns ``True`` if the file was removed.
    """
    root = repo_root or find_git_root()
    if root is None:
        return False

    target = root / ".github" / "workflows" / _WORKFLOW_NAME
    # is_symlink() catches a broken symlink that exists() would miss, so disable
    # cleans up a symlinked workflow rather than leaving it behind.
    if not target.is_file() and not target.is_symlink():
        return False

    target.unlink()
    return True


def check_ci_workflow(repo_root: Path | None = None) -> bool:
    """Check if biff-notify.yml exists and is current.

    Returns ``True`` if the workflow is deployed and matches the template.
    """
    root = repo_root or find_git_root()
    if root is None:
        return False

    target = root / ".github" / "workflows" / _WORKFLOW_NAME
    if not target.exists():
        return False

    return target.read_text(encoding="utf-8") == _template_content()
