"""Committed enablement artifacts -- the single definition of ``enable``/``disable``.

Both front-ends route here: the ``biff enable`` / ``biff disable`` CLI verbs and
the MCP ``biff`` tool (``/biff enable`` / ``/biff disable``).  Sharing one
definition is what makes the two surfaces equivalent -- the "two equivalent ways
to one state" model (DES-052, biff-j5u).

``enable`` writes two **committed** artifacts into the working tree:

- ``.punt-labs/biff/enabled`` -- the policy marker ``is_enabled()`` reads.
- ``.github/workflows/biff-notify.yml`` -- the CI notify workflow.  It runs on a
  GitHub Actions runner, which only ever does a fresh ``git checkout`` and never
  runs ``biff install``, so it must be a tracked file committed with the marker.

It deliberately never touches ``.git/hooks/`` -- those dispatchers are per-clone,
local, and never committed.  ``biff install`` deploys them and they gate on the
marker at runtime (DES-052 rule 3 / §2.11), so a marker-enabled clone without
hooks, or hooks in a not-yet-enabled clone, both behave correctly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from biff._stdlib import remove_enabled_marker, write_enabled_marker
from biff.ci_workflow import deploy_ci_workflow, remove_ci_workflow

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["EnablementChange", "RepoEnablement"]


@dataclass(frozen=True, slots=True)
class EnablementChange:
    """What an ``enable``/``disable`` run wrote or removed, for the caller to report."""

    ci_workflow_changed: bool


@final
class RepoEnablement:
    """Owns the committed enablement artifacts for one repository.

    The single source of truth for what ``enable``/``disable`` do, so the CLI
    verbs and the MCP ``biff`` tool stay byte-for-byte equivalent.  Neither
    operation runs git -- the user commits the changed files via a PR like any
    other repo change.
    """

    __slots__ = ("_root",)

    _root: Path

    def __new__(cls, repo_root: Path) -> Self:
        self = super().__new__(cls)
        self._root = repo_root
        return self

    def enable(self) -> EnablementChange:
        """Write the committed CI workflow and marker. Idempotent.

        Order is load-bearing: the CI workflow is deployed FIRST and the
        marker (which ``is_enabled`` reads) is written LAST, so if the CI
        write fails the marker is never written and the repo stays OFF --
        fail-safe rather than half-enabled with a marker but no workflow.
        """
        ci_changed = deploy_ci_workflow(self._root)
        write_enabled_marker(self._root)
        return EnablementChange(ci_workflow_changed=ci_changed)

    def disable(self) -> EnablementChange:
        """Remove the committed marker and CI workflow. Idempotent."""
        remove_enabled_marker(self._root)
        ci_changed = remove_ci_workflow(self._root)
        return EnablementChange(ci_workflow_changed=ci_changed)
