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
  the operator's opt-in, repo-level only (:mod:`biff.claude_permissions`).
- ``.punt-labs/biff/CLAUDE.md`` plus the ``@.punt-labs/biff/CLAUDE.md`` import
  in the repo ``CLAUDE.md`` -- the agent guide.  ``enable`` is the sole path that
  deposits a guide (``tool-enable-disable.md`` §2.5/§2.6); ``install`` never does.

``disable`` removes the marker, hooks, CI workflow, allow rules and the import
line.  The
deposited guide is left dormant (§2.9).  ``enable``/``disable`` invoke git only
read-only (``git rev-parse`` to resolve the hooks directory); they never create
commits or stage files -- the user commits the tracked marker + CI workflow via
a PR like any repo change, while the local hooks are activated per-clone (each
contributor runs ``biff enable`` or the superset ``biff install`` once in their
clone).  Claude Code's session/tool hooks are registered globally by the
marketplace plugin and gate on the marker at runtime, so ``enable`` never has to
touch them.
"""

from __future__ import annotations

import errno
import importlib.resources
import os
import stat
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self, final

from biff._stdlib import remove_enabled_marker, write_enabled_marker
from biff.ci_workflow import deploy_ci_workflow, remove_ci_workflow
from biff.claude_md import ClaudeMdImport
from biff.claude_permissions import PermissionChange, ProjectPermissions
from biff.git_hooks import deploy_git_hooks, remove_git_hooks, resolve_hooks_dir

if TYPE_CHECKING:
    from pathlib import Path

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

_GUIDE_RESOURCE = "user-claude.md"
_REPO_IMPORT_LINE = "@.punt-labs/biff/CLAUDE.md"
# The shared punt lock files (CLAUDE.md import, .claude/settings.json) are
# local-only; one glob keeps every punt tool's lock out of ``git status``.
_LOCK_IGNORE = "*.punt-import.lock"


@dataclass(frozen=True, slots=True)
class EnablementChange:
    """What an ``enable``/``disable`` run wrote or removed, for the caller to report.

    ``git_hooks_resolved`` is ``False`` when the git hooks directory could not
    be resolved (not a git repo, or ``git`` not on ``PATH``).  It disambiguates
    an *empty* ``git_hooks_changed`` -- "already current" (resolved, nothing to
    do) from "could not deploy anything" -- so ``enable`` never reports success
    while silently deploying zero hooks.  ``permissions`` reports the
    ``.claude/settings.json`` allow rules added or removed, or why not.
    """

    ci_workflow_changed: bool
    git_hooks_changed: tuple[str, ...]
    git_hooks_resolved: bool
    permissions: PermissionChange = field(default_factory=PermissionChange)

    @property
    def failed(self) -> bool:
        """Whether the run must be reported as a failure (non-zero exit)."""
        return bool(self.permissions.error)

    def enable_report(self) -> list[str]:
        """Return the permission lines and closing line for an ``enable`` run."""
        if self.failed:
            return [
                *self.permissions.lines("added to"),
                (
                    "biff not enabled; nothing was written. Fix "
                    ".claude/settings.json and rerun biff enable."
                ),
            ]
        return [*self.permissions.lines("added to"), ENABLED_MESSAGE]

    def disable_report(self) -> list[str]:
        """Return the permission lines and closing line for a ``disable`` run."""
        if self.failed:
            return [
                *self.permissions.lines("removed from"),
                (
                    "biff disabled, but its allow rules are still in "
                    ".claude/settings.json. Fix the file and rerun biff disable."
                ),
            ]
        return [*self.permissions.lines("removed from"), DISABLED_MESSAGE]


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
        3. **Permissions refused.** The ``.claude/settings.json`` grant runs
           first; if it is refused (malformed file, symlink, home directory)
           nothing else is written and the change reports ``failed``.
        """
        if resolve_hooks_dir(self._root) is None:
            return EnablementChange(
                ci_workflow_changed=False,
                git_hooks_changed=(),
                git_hooks_resolved=False,
            )
        permissions = ProjectPermissions(self._root).grant()
        if permissions.error:
            return EnablementChange(
                ci_workflow_changed=False,
                git_hooks_changed=(),
                git_hooks_resolved=True,
                permissions=permissions,
            )
        try:
            ci_changed = deploy_ci_workflow(self._root)
            hooks_changed = tuple(deploy_git_hooks(self._root))
            self._deposit_guide()
            self._guide_import().register()
            self._ignore_locks()
            write_enabled_marker(self._root)
        except BaseException:
            ProjectPermissions(self._root).revoke(permissions.changed)
            raise
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
        remove_enabled_marker(self._root)
        permissions = ProjectPermissions(self._root).revoke()
        hooks_changed = tuple(remove_git_hooks(self._root))
        ci_changed = remove_ci_workflow(self._root)
        self._guide_import().prune()
        return EnablementChange(
            ci_workflow_changed=ci_changed,
            git_hooks_changed=hooks_changed,
            git_hooks_resolved=resolved,
            permissions=permissions,
        )

    def _ignore_locks(self) -> None:
        """Append :data:`_LOCK_IGNORE` to the repo ``.gitignore`` once.

        Opened ``O_NOFOLLOW``: a symlinked ``.gitignore`` is left alone (the
        locks then merely show as untracked) rather than written through.
        """
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(self._root / ".gitignore", flags, 0o644)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                return
            raise
        with os.fdopen(fd, "r+b") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return
            text = handle.read().decode("utf-8", errors="replace")
            if _LOCK_IGNORE in (line.strip() for line in text.splitlines()):
                return
            sep = "\n" if text and not text.endswith("\n") else ""
            handle.write(f"{sep}{_LOCK_IGNORE}\n".encode())

    def _guide_import(self) -> ClaudeMdImport:
        return ClaudeMdImport(self._root / "CLAUDE.md", _REPO_IMPORT_LINE)

    def _deposit_guide(self) -> None:
        """Write the bundled guide to ``.punt-labs/biff/CLAUDE.md`` wholesale.

        Overwrites (§2.2 vendored zone); skips the write when the bytes already
        match so re-running ``enable`` never churns the file.  The compare is on
        bytes so a tampered non-UTF-8 guide is overwritten, not a crash.
        """
        want = (
            importlib.resources.files("biff.data")
            .joinpath(_GUIDE_RESOURCE)
            .read_bytes()
        )
        guide = self._root / ".punt-labs" / "biff" / "CLAUDE.md"
        if guide.is_file() and guide.read_bytes() == want:
            return
        guide.parent.mkdir(parents=True, exist_ok=True)
        guide.write_bytes(want)
