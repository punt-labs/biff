"""CI workflow deployment for biff.

Deploys a standalone ``biff-notify.yml`` workflow that fires on
``workflow_run`` completion and posts a ``biff wall`` on failure.
No existing workflow files are touched.
"""

from __future__ import annotations

import importlib.resources
import json
from pathlib import Path
from typing import Self, cast, final

import yaml

from biff._stdlib import ensure_real_dir, is_regular_file
from biff.config import find_git_root

_WORKFLOW_NAME = "biff-notify.yml"

# The notify workflow's own ``name:`` -- excluded from the watched list so it
# never triggers itself (a workflow_run on its own completion).
_NOTIFY_WORKFLOW_NAME = "Biff CI Notifications"

# The line in the bundled template that carries the watched-workflows list.
# ``NotifyWorkflow.render`` swaps it for a list parameterized to the target
# repo -- the template value is a placeholder, never deposited verbatim.
_WORKFLOWS_INDENT = "    "
_WORKFLOWS_PLACEHOLDER = f"{_WORKFLOWS_INDENT}workflows: []  # BIFF_RENDER"

# GitHub Actions reads workflows with either extension.
_WORKFLOW_SUFFIXES = frozenset({".yml", ".yaml"})


def _template_content() -> str:
    """Read the bundled workflow template (with the unrendered placeholder)."""
    return (
        importlib.resources.files("biff.data")
        .joinpath(_WORKFLOW_NAME)
        .read_text(encoding="utf-8")
    )


@final
class NotifyWorkflow:
    """The per-repo render of the ``biff-notify.yml`` CI-notification workflow.

    ``workflow_run`` matches a workflow's ``name:`` field, so the trigger list
    must name the TARGET repo's own workflows -- a fixed list (the retired
    ``["Lint", "Tests", "Docs"]``) watched the wrong set in every repo but the
    one it was written for.  :meth:`render` substitutes the bundled template's
    placeholder line with the sorted, unique ``name:`` fields found in
    ``<root>/.github/workflows`` (excluding this workflow itself), so a repo
    whose workflow names change re-renders on the next ``biff enable``.
    """

    __slots__ = ("_root",)

    _root: Path

    def __new__(cls, repo_root: Path) -> Self:
        self = super().__new__(cls)
        self._root = repo_root
        return self

    def render(self) -> str:
        """Return the deposited workflow content for this repo."""
        return _template_content().replace(
            _WORKFLOWS_PLACEHOLDER, self._workflows_block()
        )

    def _workflows_block(self) -> str:
        """Render the ``workflows:`` line (with a comment when nothing to watch)."""
        names = self._watched_names()
        if not names:
            return (
                f"{_WORKFLOWS_INDENT}# No sibling workflows to watch yet; "
                "re-run `biff enable` after adding one.\n"
                f"{_WORKFLOWS_INDENT}workflows: []"
            )
        # json.dumps yields a JSON array of double-quoted strings, which is a
        # valid YAML flow sequence and safely quotes names with odd characters.
        return f"{_WORKFLOWS_INDENT}workflows: {json.dumps(list(names))}"

    def _watched_names(self) -> tuple[str, ...]:
        """Sorted, unique workflow ``name:`` fields, minus the notify workflow."""
        workflows_dir = self._root / ".github" / "workflows"
        names: set[str] = set()
        for path in workflows_dir.glob("*"):
            if path.name == _WORKFLOW_NAME or path.suffix not in _WORKFLOW_SUFFIXES:
                continue
            name = self._workflow_name(path)
            if name is not None and name != _NOTIFY_WORKFLOW_NAME:
                names.add(name)
        return tuple(sorted(names))

    def _workflow_name(self, path: Path) -> str | None:
        """Return a workflow file's top-level ``name:`` field, or ``None``.

        ``None`` is the documented contract for "no watchable name": a
        malformed or nameless workflow is skipped rather than aborting the whole
        render, since this reads arbitrary committed files in the target repo.
        """
        try:
            raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            return None
        if isinstance(raw, dict):
            name = cast("dict[str, object]", raw).get("name")
            if isinstance(name, str):
                return name
        return None


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
    # Rendered against THIS repo's workflow names (see NotifyWorkflow): a repo
    # whose names changed re-renders on the next enable rather than keeping a
    # stale watched list.  ensure_real_dir ran first, so the workflows dir the
    # render scans exists (empty -> empty watched list).
    rendered = NotifyWorkflow(root).render()

    # Symlink guard (mirrors write_enabled_marker): an untrusted checkout could
    # commit biff-notify.yml as a symlink; replace it with a real file rather
    # than following it and clobbering the link's target. Done before the
    # freshness read so a symlink is never left in place as "up to date".
    if target.is_symlink():
        target.unlink()

    if is_regular_file(target) and target.read_text(encoding="utf-8") == rendered:
        return False  # Already up to date

    target.write_text(rendered, encoding="utf-8")
    return True


def remove_ci_workflow(repo_root: Path | None = None) -> bool:
    """Remove biff-notify.yml from ``.github/workflows/``.

    Returns ``True`` if the file was removed.
    """
    root = repo_root or find_git_root()
    if root is None:
        return False

    target = root / ".github" / "workflows" / _WORKFLOW_NAME
    # Regular file OR symlink → remove it. unlink() removes a symlink without
    # following it, so a committed symlinked workflow is cleaned up rather than
    # its target being touched. Anything else at the path (absent, or a
    # directory we don't own) → nothing of ours to remove; leave it, report
    # False, and never misreport a directory as removed.
    if target.is_symlink() or is_regular_file(target):
        target.unlink()
        return True
    return False


def check_ci_workflow(repo_root: Path | None = None) -> bool:
    """Check if biff-notify.yml exists and is current.

    Returns ``True`` when the deployed file matches the render for THIS repo --
    so a repo whose workflow names changed reports not-current and re-renders
    on the next ``deploy_ci_workflow``.
    """
    root = repo_root or find_git_root()
    if root is None:
        return False

    target = root / ".github" / "workflows" / _WORKFLOW_NAME
    # Regular file only: a symlinked (or otherwise non-regular) workflow path is
    # not ours — don't follow it to read an arbitrary target; report not-current.
    if not is_regular_file(target):
        return False

    return target.read_text(encoding="utf-8") == NotifyWorkflow(root).render()
