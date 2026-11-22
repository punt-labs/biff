"""Enablement -- the single definition of ``enable``/``disable``.

Both front-ends route here: the ``biff enable`` / ``biff disable`` CLI verbs and
the MCP ``biff`` tool (``/biff enable`` / ``/biff disable``).  Sharing one
definition is what makes the two surfaces equivalent -- the "two equivalent ways
to one state" model (DES-052).

``enable`` fully activates the current clone in one verb (the beads ``bd setup``
model), writing five artifacts:

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
- ``.claude/settings.json`` allow rules for biff's MCP tools and commands --
  the operator's opt-in, repo-level only
  (:mod:`biff.plugins.claude_code.claude_permissions`).
- ``.punt-labs/biff/CLAUDE.md`` plus the ``@.punt-labs/biff/CLAUDE.md`` import
  in the repo ``CLAUDE.md`` -- the agent guide.  ``enable`` is the sole path that
  deposits a guide (``tool-enable-disable.md`` §2.5/§2.6); ``install`` never does.

``disable`` removes the marker, hooks, CI workflow, allow rules and the import
line.  The deposited guide is left dormant (§2.9).  ``enable``/``disable`` invoke
git only read-only (``git rev-parse`` to resolve the hooks directory); they never
create commits or stage files -- the user commits the tracked marker + CI workflow via
a PR like any repo change, while the local hooks are activated per-clone (each
contributor runs ``biff enable`` or the superset ``biff install`` once in their
clone).  Claude Code's session/tool hooks are registered globally by the
marketplace plugin and gate on the marker at runtime, so ``enable`` never has to
touch them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from biff._enablement_artifacts import CheckoutArtifacts
from biff.ci_workflow import deploy_ci_workflow, remove_ci_workflow
from biff.git_hooks import deploy_git_hooks, remove_git_hooks, resolve_hooks_dir

if TYPE_CHECKING:
    from pathlib import Path

    from biff.plugins.contract import Installer, PermissionChange, PluginRegistry

__all__ = ["DISABLED_MESSAGE", "ENABLED_MESSAGE", "EnablementChange", "RepoEnablement"]

ENABLED_MESSAGE = (
    "biff enabled. Commit .punt-labs/biff/enabled, "
    ".github/workflows/biff-notify.yml, .punt-labs/biff/CLAUDE.md, the "
    "@.punt-labs/biff/CLAUDE.md import added to CLAUDE.md, .claude/settings.json, "
    "and .gitignore, then restart Claude Code for changes to take effect."
)
DISABLED_MESSAGE = (
    "biff disabled. Commit the removal of .punt-labs/biff/enabled, "
    ".github/workflows/biff-notify.yml, the @.punt-labs/biff/CLAUDE.md import "
    "from CLAUDE.md, and biff's rules from .claude/settings.json, then restart "
    "Claude Code for changes to take effect."
)


@dataclass(frozen=True, slots=True)
class EnablementChange:
    """What an ``enable``/``disable`` run wrote or removed, for the caller to report.

    ``git_hooks_resolved`` is ``False`` when the git hooks directory could not
    be resolved (not a git repo, or ``git`` not on ``PATH``).  It disambiguates
    an *empty* ``git_hooks_changed`` -- "already current" (resolved, nothing to
    do) from "could not deploy anything" -- so ``enable`` never reports success
    while silently deploying zero hooks.  ``permissions`` reports the
    allow rules each registered tool's installer added or removed, or why not.
    """

    ci_workflow_changed: bool
    git_hooks_changed: tuple[str, ...]
    git_hooks_resolved: bool
    permissions: tuple[PermissionChange, ...] = ()

    @property
    def failed(self) -> bool:
        """Whether the run must be reported as a failure (non-zero exit)."""
        return any(c.error for c in self.permissions)

    def enable_report(self) -> list[str]:
        """Return the permission lines and closing line for an ``enable`` run."""
        if self.failed:
            fix = next(c.fix for c in self.permissions if c.error)
            return [
                *self._lines("added to"),
                f"biff not enabled; nothing was written. {fix} and rerun biff enable.",
            ]
        return [*self._lines("added to"), ENABLED_MESSAGE]

    def disable_report(self) -> list[str]:
        """Return the permission lines and closing line for a ``disable`` run."""
        closing = DISABLED_MESSAGE
        if self.failed:
            closing = (
                "biff disabled, but its allow rules are still in "
                ".claude/settings.json. Fix the file and rerun biff disable."
            )
        return [*self._lines("removed from"), closing]

    def _lines(self, verb: str) -> list[str]:
        return [line for c in self.permissions for line in c.lines(verb)]


@final
class RepoEnablement:
    """Owns the enablement artifacts for one repository.

    The single source of truth for what ``enable``/``disable`` do, so the CLI
    verbs and the MCP ``biff`` tool stay byte-for-byte equivalent.  They invoke
    git only read-only (to resolve the hooks directory) and never create
    commits -- the user commits the tracked marker + CI workflow via a PR like
    any other repo change; the local git hooks are per-clone machinery.
    """

    __slots__ = ("_plugins", "_root")

    _root: Path
    _plugins: PluginRegistry

    def __new__(cls, repo_root: Path, plugins: PluginRegistry) -> Self:
        """Create an enablement manager; *plugins* holds each tool's installer."""
        self = super().__new__(cls)
        self._root = repo_root
        self._plugins = plugins
        return self

    def enable(self) -> EnablementChange:
        """Fully activate this clone: permissions, CI workflow, git hooks, marker.

        Fails safe three ways, so a half-activated repo is never left behind:

        1. **Unresolvable hooks dir.** If the git hooks directory cannot be
           resolved (not a git repo, or ``git`` not on ``PATH``), enable writes
           *nothing* and returns ``git_hooks_resolved=False``; the caller emits
           the shared NOTICE instead of claiming success.
        2. **Write failure.** On the happy path the marker (which
           ``is_enabled`` reads) is written LAST, so any exception raised while
           deploying the CI workflow or the git hooks leaves the marker absent
           and the repo cleanly OFF. (``deploy_ci_workflow`` returning ``False``
           is a no-op "already current", not a failure -- real failures raise.)
           The allow rules granted first are revoked again before re-raising.
        3. **Permissions refused.** Each tool's grant runs first; if one is
           refused (malformed file, symlink, home directory) the earlier
           grants are revoked, nothing else is written, and the change
           reports ``failed``.
        """
        if resolve_hooks_dir(self._root) is None:
            return EnablementChange(
                ci_workflow_changed=False,
                git_hooks_changed=(),
                git_hooks_resolved=False,
            )
        grants, refusals = self._grant_all()
        if refusals:
            return EnablementChange(
                ci_workflow_changed=False,
                git_hooks_changed=(),
                git_hooks_resolved=True,
                permissions=refusals,
            )
        permissions = tuple(c for _, c in grants)
        try:
            ci_changed = deploy_ci_workflow(self._root)
            hooks_changed = tuple(deploy_git_hooks(self._root))
            changes = self._enable_artifacts()
        except BaseException:
            self._revoke(grants)
            raise
        CheckoutArtifacts.report(
            "enable",
            {
                "workflow": ci_changed,
                "hooks": bool(hooks_changed),
                "permissions": any(c.changed for c in permissions),
                **changes,
            },
        )
        return EnablementChange(
            ci_workflow_changed=ci_changed,
            git_hooks_changed=hooks_changed,
            git_hooks_resolved=True,
            permissions=permissions,
        )

    def disable(self) -> EnablementChange:
        """Deactivate this clone: remove the marker, git hooks, and CI workflow.

        Removes exactly what :meth:`enable` added, permission rules included.
        The marker is removed
        first, so the repo reads OFF immediately even if a later step fails, and
        the allow rules are revoked next -- before any step that can raise -- so
        a hooks or CI failure cannot strand them.  A refused revoke reports
        ``failed`` rather than success.
        Idempotent.  Proceeds even when the hooks dir is unresolvable (there is
        simply nothing local to remove); ``git_hooks_resolved`` still reports
        whether it resolved, for symmetry with :meth:`enable`.
        """
        resolved = resolve_hooks_dir(self._root) is not None
        marker_changed = self._remove_marker()
        installers = self._plugins.installers()
        permissions = tuple(i.revoke(self._root) for i in installers)
        hooks_changed = tuple(remove_git_hooks(self._root))
        ci_changed = remove_ci_workflow(self._root)
        import_changed = sum(i.unregister_guide(self._root) for i in installers) > 0
        CheckoutArtifacts.report(
            "disable",
            {
                "marker": marker_changed,
                "workflow": ci_changed,
                "hooks": bool(hooks_changed),
                "permissions": any(c.changed for c in permissions),
                "guide_import": import_changed,
            },
        )
        return EnablementChange(
            ci_workflow_changed=ci_changed,
            git_hooks_changed=hooks_changed,
            git_hooks_resolved=resolved,
            permissions=permissions,
        )

    def _enable_artifacts(self) -> dict[str, bool]:
        artifacts = CheckoutArtifacts(self._root)
        return {
            "guide": artifacts.deposit_guide(),
            # sum, not any: every installer registers, none is short-circuited.
            "guide_import": sum(
                i.register_guide(self._root) for i in self._plugins.installers()
            )
            > 0,
            "ignore": artifacts.ignore_locks(),
            "marker": artifacts.write_marker(),
        }

    def _remove_marker(self) -> bool:
        return CheckoutArtifacts(self._root).remove_marker()

    def _grant_all(
        self,
    ) -> tuple[list[tuple[Installer, PermissionChange]], tuple[PermissionChange, ...]]:
        """Preflight, then grant, every tool; return the grants or the refusals.

        A refusal at either step writes nothing further and revokes the grants
        already made.
        """
        installers = self._plugins.installers()
        refusals = tuple(
            c for c in (i.preflight(self._root) for i in installers) if c.error
        )
        grants: list[tuple[Installer, PermissionChange]] = []
        if refusals:
            return grants, refusals
        for installer in installers:
            granted = installer.grant(self._root)
            if granted.error:
                self._revoke(grants)
                return [], (granted,)
            grants.append((installer, granted))
        return grants, ()

    def _revoke(self, grants: list[tuple[Installer, PermissionChange]]) -> None:
        for installer, granted in grants:
            installer.revoke(self._root, granted.changed)
