"""CI workflow deployment for biff.

Deploys a standalone ``biff-notify.yml`` workflow that fires on
``workflow_run`` completion and posts a ``biff wall`` on failure.
No existing workflow files are touched.
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path

from biff.config import find_git_root

_WORKFLOW_NAME = "biff-notify.yml"


def _template_content() -> str:
    """Read the bundled workflow template."""
    return importlib.resources.files("biff.data").joinpath(_WORKFLOW_NAME).read_text()


def deploy_ci_workflow(repo_root: Path | None = None) -> bool:
    """Deploy biff-notify.yml to ``.github/workflows/``.

    Returns ``True`` if the file was created or updated.
    """
    root = repo_root or find_git_root()
    if root is None or not root.is_dir():
        return False

    workflows_dir = root / ".github" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)

    target = workflows_dir / _WORKFLOW_NAME
    template = _template_content()

    if target.exists() and target.read_text() == template:
        return False  # Already up to date

    target.write_text(template)
    return True


def remove_ci_workflow(repo_root: Path | None = None) -> bool:
    """Remove biff-notify.yml from ``.github/workflows/``.

    Returns ``True`` if the file was removed.
    """
    root = repo_root or find_git_root()
    if root is None:
        return False

    target = root / ".github" / "workflows" / _WORKFLOW_NAME
    if not target.exists():
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

    return target.read_text() == _template_content()
