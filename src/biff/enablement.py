"""Enablement -- the single definition of ``enable``/``disable``.

Both front-ends route here: the ``biff enable`` / ``biff disable`` CLI verbs and
the MCP ``biff`` tool (``/biff enable`` / ``/biff disable``).  Sharing one
definition is what makes the two surfaces equivalent -- the "two equivalent ways
to one state" model (DES-052).

``enable`` fully activates the current clone in one verb (the beads ``bd setup``
model), writing three artifacts:

- ``.punt-labs/biff/enabled`` -- the **committed** policy marker ``is_enabled()``
  reads; commit it via a PR so the whole team's repo is on.
- ``.github/workflows/biff-notify.yml`` -- the **committed** CI notify workflow.
  It runs on a GitHub Actions runner, which only ever does a fresh ``git
  checkout`` and never runs ``biff install``, so it must be a tracked file.
- ``.git/hooks/`` biff dispatchers -- **per-clone, local, never committed**
  machinery.  They are resolved with ``git rev-parse`` so worktrees and
  ``core.hooksPath`` are honored, and they gate on the marker at runtime, so
  deploying them in a not-yet-enabled clone is a safe no-op until the marker
  lands (§2.11).

``disable`` removes exactly those three.  ``enable``/``disable`` invoke git only
read-only (``git rev-parse`` to resolve the hooks directory); they never create
commits or stage files -- the user commits the tracked marker + CI workflow via
a PR like any repo change, while the local hooks are activated per-clone (each
contributor runs ``biff enable`` or the superset ``biff install`` once in their
clone).  Claude Code's session/tool hooks are registered globally by the
marketplace plugin and gate on the marker at runtime, so ``enable`` never has to
touch them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from biff._stdlib import remove_enabled_marker, write_enabled_marker
from biff.ci_workflow import deploy_ci_workflow, remove_ci_workflow
from biff.git_hooks import deploy_git_hooks, remove_git_hooks, resolve_hooks_dir

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["EnablementChange", "RepoEnablement"]


@dataclass(frozen=True, slots=True)
class EnablementChange:
    """What an ``enable``/``disable`` run wrote or removed, for the caller to report.

    ``git_hooks_resolved`` is ``False`` when the git hooks directory could not
    be resolved (not a git repo, or ``git`` not on ``PATH``).  It disambiguates
    an *empty* ``git_hooks_changed`` -- "already current" (resolved, nothing to
    do) from "could not deploy anything" -- so ``enable`` never reports success
    while silently deploying zero hooks.
    """

    ci_workflow_changed: bool
    git_hooks_changed: tuple[str, ...]
    git_hooks_resolved: bool


@final
class RepoEnablement:
    """Owns the enablement artifacts for one repository.

    The single source of truth for what ``enable``/``disable`` do, so the CLI
    verbs and the MCP ``biff`` tool stay byte-for-byte equivalent.  They invoke
    git only read-only (to resolve the hooks directory) and never create
    commits -- the user commits the tracked marker + CI workflow via a PR like
    any other repo change; the local git hooks are per-clone machinery.
    """

    __slots__ = ("_root",)

    _root: Path

    def __new__(cls, repo_root: Path) -> Self:
        self = super().__new__(cls)
        self._root = repo_root
        return self

    def enable(self) -> EnablementChange:
        """Fully activate this clone: CI workflow, git hooks, then marker.

        Fails safe two ways, so a half-activated repo is never left behind:

        1. **Unresolvable hooks dir.** If the git hooks directory cannot be
           resolved (not a git repo, or ``git`` not on ``PATH``), enable writes
           *nothing* and returns ``git_hooks_resolved=False``; the caller emits
           the shared NOTICE instead of claiming success.
        2. **Write failure.** On the happy path the marker (which
           ``is_enabled`` reads) is written LAST, so any exception raised while
           deploying the CI workflow or the git hooks leaves the marker absent
           and the repo cleanly OFF. (``deploy_ci_workflow`` returning ``False``
           is a no-op "already current", not a failure -- real failures raise.)
        """
        if resolve_hooks_dir(self._root) is None:
            return EnablementChange(
                ci_workflow_changed=False,
                git_hooks_changed=(),
                git_hooks_resolved=False,
            )
        ci_changed = deploy_ci_workflow(self._root)
        hooks_changed = tuple(deploy_git_hooks(self._root))
        write_enabled_marker(self._root)
        return EnablementChange(
            ci_workflow_changed=ci_changed,
            git_hooks_changed=hooks_changed,
            git_hooks_resolved=True,
        )

    def disable(self) -> EnablementChange:
        """Deactivate this clone: remove the marker, git hooks, and CI workflow.

        Removes exactly what :meth:`enable` added.  The marker is removed
        first, so the repo reads OFF immediately even if a later step fails.
        Idempotent.  Proceeds even when the hooks dir is unresolvable (there is
        simply nothing local to remove); ``git_hooks_resolved`` still reports
        whether it resolved, for symmetry with :meth:`enable`.
        """
        resolved = resolve_hooks_dir(self._root) is not None
        remove_enabled_marker(self._root)
        hooks_changed = tuple(remove_git_hooks(self._root))
        ci_changed = remove_ci_workflow(self._root)
        return EnablementChange(
            ci_workflow_changed=ci_changed,
            git_hooks_changed=hooks_changed,
            git_hooks_resolved=resolved,
        )
