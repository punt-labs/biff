"""biff's allow rules in a project's ``.claude/settings.json``.

``biff enable`` is the operator's explicit opt-in, so it -- not plugin startup
-- grants Claude Code permission to call biff's MCP tools and slash commands
without a prompt. The rules land in the enabled repo's
``<repo>/.claude/settings.json`` only, never the user-global
``~/.claude/settings.json``, and ``biff disable`` removes exactly the same set
(punt-kit ``permissions.md`` §6). The ``biff-dev`` plugin's rules are never
granted: maintainers add them by hand.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Self, cast, final

__all__ = ["BIFF_RULES", "PermissionChange", "ProjectPermissions"]

_COMMANDS: Final = (
    "biff",
    "finger",
    "last",
    "mesg",
    "plan",
    "read",
    "talk",
    "tty",
    "wall",
    "who",
    "write",
)

# The deterministic rule set: enable adds every missing member, disable removes
# these exact values. A rule retired here strands on disable, so it stays in the
# set until no enabled repo can still carry it.
BIFF_RULES: Final = (
    "mcp__plugin_biff_tty__*",
    "Bash(biff:*)",
    "Skill(biff:*)",
    *(f"Skill({name})" for name in _COMMANDS),
)

_NEW_FILE_MODE: Final = 0o644


@dataclass(frozen=True, slots=True)
class PermissionChange:
    """What a grant or revoke did to the project's ``.claude/settings.json``.

    ``changed`` lists the rules added (grant) or removed (revoke); an empty
    tuple with no ``error`` means the file already matched. ``error`` explains
    why the file was left untouched.
    """

    changed: tuple[str, ...] = ()
    error: str = ""

    def lines(self, verb: str) -> list[str]:
        """Return a report line for the CLI and MCP surfaces, or none."""
        if self.error:
            return [f"Permissions not changed: {self.error}"]
        if self.changed:
            rules = ", ".join(self.changed)
            return [f"Permissions {verb} .claude/settings.json: {rules}"]
        return []


@final
class ProjectPermissions:
    """Add or remove :data:`BIFF_RULES` in one repo's ``.claude/settings.json``.

    The read-modify-write holds the tool-agnostic
    ``.settings.json.punt-import.lock`` (other punt tools edit the same file),
    refuses a symlinked ``.claude/`` or ``settings.json`` so a planted link
    cannot redirect the write out of the repo, appends in rule order without
    reordering existing entries, and does not write when nothing changes.
    Every other key and rule in the file is preserved.
    """

    __slots__ = ("_dir", "_file", "_root")

    _dir: Path
    _file: Path
    _root: Path

    def __new__(cls, root: Path) -> Self:
        self = super().__new__(cls)
        self._root = root
        self._dir = root / ".claude"
        self._file = self._dir / "settings.json"
        return self

    def grant(self) -> PermissionChange:
        """Append each missing biff rule; return the rules added."""
        return self._update(add=True)

    def revoke(self) -> PermissionChange:
        """Remove every biff rule; return the rules removed."""
        return self._update(add=False)

    def _update(self, *, add: bool) -> PermissionChange:
        """Apply the grant or revoke under the lock, reporting any refusal."""
        if self._root.resolve() == Path.home().resolve():
            return PermissionChange(
                error="the repo is the home directory, whose .claude/"
                "settings.json is user-global"
            )
        try:
            if not self._settings_present() and not add:
                return PermissionChange()
            self._dir.mkdir(exist_ok=True)
            lock_path = self._dir / ".settings.json.punt-import.lock"
            flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
            with os.fdopen(os.open(lock_path, flags, 0o600), "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                return PermissionChange(changed=self._apply(add=add))
        except ValueError as exc:
            return PermissionChange(error=str(exc))
        except OSError as exc:
            return PermissionChange(error=exc.strerror or type(exc).__name__)

    def _settings_present(self) -> bool:
        """Return whether ``settings.json`` exists; refuse symlinks.

        Runs before the lock is taken: the lock file lives in ``.claude/``, so
        a ``.claude`` symlink would place it outside the repo, and a revoke
        with no settings file must not create ``.claude/``.
        """
        for path, name in ((self._dir, ".claude"), (self._file, "settings.json")):
            try:
                mode = os.lstat(path).st_mode
            except FileNotFoundError:
                return False
            if stat.S_ISLNK(mode):
                msg = f"{name} is a symlink; refusing to edit through it"
                raise ValueError(msg)
        return True

    def _apply(self, *, add: bool) -> tuple[str, ...]:
        """Edit the allow list in place and write it back if it changed."""
        settings = self._read()
        allow = self._allow_list(settings)
        if add:
            changed = tuple(rule for rule in BIFF_RULES if rule not in allow)
            allow.extend(changed)
        else:
            changed = tuple(rule for rule in BIFF_RULES if rule in allow)
            allow[:] = [rule for rule in allow if rule not in BIFF_RULES]
        if changed:
            self._write(json.dumps(settings, indent=2) + "\n")
        return changed

    def _read(self) -> dict[str, object]:
        """Return the parsed settings, ``{}`` when absent; refuse a non-object."""
        try:
            data: object = json.loads(self._file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            msg = f".claude/settings.json is not valid JSON ({exc.msg})"
            raise ValueError(msg) from exc
        except UnicodeDecodeError as exc:
            msg = ".claude/settings.json is not valid UTF-8"
            raise ValueError(msg) from exc
        if not isinstance(data, dict):
            msg = ".claude/settings.json is not a JSON object"
            raise ValueError(msg)
        return cast("dict[str, object]", data)

    @staticmethod
    def _allow_list(settings: dict[str, object]) -> list[object]:
        """Return ``permissions.allow``, creating it if absent; refuse bad shapes.

        A ``permissions`` or ``allow`` of the wrong type is the user's data, so
        it is reported rather than overwritten.
        """
        permissions = settings.setdefault("permissions", {})
        if not isinstance(permissions, dict):
            msg = ".claude/settings.json 'permissions' is not an object"
            raise ValueError(msg)
        allow = cast("dict[str, object]", permissions).setdefault("allow", [])
        if not isinstance(allow, list):
            msg = ".claude/settings.json 'permissions.allow' is not a list"
            raise ValueError(msg)
        return cast("list[object]", allow)

    def _write(self, text: str) -> None:
        """Replace ``settings.json`` atomically, keeping an existing mode."""
        mode = (
            stat.S_IMODE(self._file.stat().st_mode)
            if self._file.is_file()
            else _NEW_FILE_MODE
        )
        fd, tmp_name = tempfile.mkstemp(
            dir=self._dir, prefix=".settings.json.", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.chmod(mode)
            tmp.replace(self._file)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise
