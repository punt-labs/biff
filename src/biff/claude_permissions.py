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
import errno
import fcntl
import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Self, cast, final

if TYPE_CHECKING:
    from collections.abc import Iterable

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

# Least privilege (permissions.md §6): only the read/messaging data plane runs
# without a prompt. The machine- and repo-mutating verbs -- install, uninstall,
# install-statusline, uninstall-statusline, enable, disable, serve, mcp, hook --
# keep prompting, so no ``Bash(biff:*)`` wildcard is granted.
_CLI_SUBCOMMANDS: Final = (
    "doctor",
    "finger",
    "last",
    "mesg",
    "plan",
    "read",
    "status",
    "statusline",
    "talk",
    "tty",
    "version",
    "wall",
    "who",
    "write",
)

# The deterministic rule set: enable adds every missing member, disable removes
# these exact values. A rule retired here strands on disable, so it stays in the
# set until no enabled repo can still carry it.
BIFF_RULES: Final = (
    "mcp__plugin_biff_tty__*",
    *(f"Bash(biff {name}:*)" for name in _CLI_SUBCOMMANDS),
    "Skill(biff:*)",
    *(f"Skill({name})" for name in _COMMANDS),
)

_NEW_FILE_MODE: Final = 0o644
_SETTINGS: Final = "settings.json"
_LOCK: Final = ".settings.json.punt-import.lock"
_NOFOLLOW: Final = os.O_NOFOLLOW | os.O_CLOEXEC


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
        """Return one report line per changed rule, the refusal, or none."""
        if self.error:
            return [f"Permissions not changed: {self.error}"]
        return [f"Permission {verb} .claude/settings.json: {r}" for r in self.changed]


@final
class ProjectPermissions:
    """Add or remove :data:`BIFF_RULES` in one repo's ``.claude/settings.json``.

    Every filesystem step runs relative to a ``.claude`` directory descriptor
    opened with ``O_NOFOLLOW``, so a symlinked ``.claude/``, ``settings.json``
    or lock file -- or one swapped in mid-run -- cannot redirect the write out
    of the repo. The read-modify-write holds the tool-agnostic
    ``.settings.json.punt-import.lock`` (other punt tools edit the same file;
    it is opened without truncation), appends in rule order without reordering
    existing entries, and does not write when nothing changes. Every other key
    and rule in the file is preserved.
    """

    __slots__ = ("_root",)

    _root: Path

    def __new__(cls, root: Path) -> Self:
        self = super().__new__(cls)
        self._root = root
        return self

    def grant(self) -> PermissionChange:
        """Append each missing biff rule; return the rules added."""
        return self._update(BIFF_RULES, add=True)

    def revoke(self, rules: Iterable[str] = BIFF_RULES) -> PermissionChange:
        """Remove *rules* (default: every biff rule); return the rules removed."""
        selected = set(rules)
        return self._update(tuple(r for r in BIFF_RULES if r in selected), add=False)

    def _update(self, rules: tuple[str, ...], *, add: bool) -> PermissionChange:
        """Apply the grant or revoke under the lock, reporting any refusal."""
        if self._root.resolve() == Path.home().resolve():
            return PermissionChange(
                error="the repo is the home directory, whose .claude/"
                "settings.json is user-global"
            )
        if not rules:
            return PermissionChange()
        try:
            root_fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                dir_fd = _open_claude_dir(root_fd, create=add)
                if dir_fd is None:
                    return PermissionChange()
                try:
                    return PermissionChange(
                        changed=_locked_apply(dir_fd, rules, add=add)
                    )
                finally:
                    os.close(dir_fd)
            finally:
                os.close(root_fd)
        except ValueError as exc:
            return PermissionChange(error=str(exc))
        except OSError as exc:
            return PermissionChange(error=exc.strerror or type(exc).__name__)


def _refuse_link(exc: OSError, name: str) -> ValueError:
    """Translate an ``O_NOFOLLOW`` refusal into a user-facing reason."""
    if exc.errno == errno.ELOOP:
        return ValueError(f"{name} is a symlink; refusing to edit through it")
    if exc.errno == errno.ENOTDIR:
        return ValueError(f"{name} is not a directory")
    raise exc


def _open_claude_dir(root_fd: int, *, create: bool) -> int | None:
    """Open ``.claude`` without following a link; ``None`` if absent on revoke."""
    if create:
        with contextlib.suppress(FileExistsError):
            os.mkdir(".claude", 0o755, dir_fd=root_fd)
    flags = os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW
    try:
        return os.open(".claude", flags, dir_fd=root_fd)
    except FileNotFoundError:
        if create:
            raise
        return None
    except OSError as exc:
        # Linux reports O_DIRECTORY|O_NOFOLLOW on a link as ENOTDIR, not ELOOP.
        if stat.S_ISLNK(
            os.stat(".claude", dir_fd=root_fd, follow_symlinks=False).st_mode
        ):
            exc.errno = errno.ELOOP
        raise _refuse_link(exc, ".claude") from exc


def _locked_apply(dir_fd: int, rules: tuple[str, ...], *, add: bool) -> tuple[str, ...]:
    """Run :func:`_apply` holding the shared lock in ``.claude``."""
    if not add and not _exists(dir_fd, _SETTINGS):
        return ()
    # Refuse a bad file before creating the lock, so a refusal leaves nothing
    # behind; the locked pass below re-validates.
    _allow_list(_read(dir_fd)[0])
    try:
        lock = os.open(_LOCK, os.O_RDWR | os.O_CREAT | _NOFOLLOW, 0o600, dir_fd=dir_fd)
    except OSError as exc:
        raise _refuse_link(exc, f".claude/{_LOCK}") from exc
    try:
        if not stat.S_ISREG(os.fstat(lock).st_mode):
            msg = f".claude/{_LOCK} is not a regular file"
            raise ValueError(msg)
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _apply(dir_fd, rules, add=add)
    finally:
        os.close(lock)


def _exists(dir_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _apply(dir_fd: int, rules: tuple[str, ...], *, add: bool) -> tuple[str, ...]:
    """Edit the allow list in place and write it back if it changed."""
    settings, mode = _read(dir_fd)
    allow = _allow_list(settings)
    if add:
        changed = tuple(rule for rule in rules if rule not in allow)
        allow.extend(changed)
    else:
        changed = tuple(rule for rule in rules if rule in allow)
        allow[:] = [rule for rule in allow if rule not in changed]
    if changed:
        _write(dir_fd, json.dumps(settings, indent=2) + "\n", mode)
    return changed


def _read(dir_fd: int) -> tuple[dict[str, object], int]:
    """Return the parsed settings and file mode; ``{}`` when absent.

    Refuses a symlink, a non-regular file, and a non-object.
    """
    try:
        fd = os.open(_SETTINGS, os.O_RDONLY | os.O_NONBLOCK | _NOFOLLOW, dir_fd=dir_fd)
    except FileNotFoundError:
        return {}, _NEW_FILE_MODE
    except OSError as exc:
        raise _refuse_link(exc, _SETTINGS) from exc
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            msg = ".claude/settings.json is not a regular file"
            raise ValueError(msg)
        raw = handle.read()
    try:
        data: object = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        msg = f".claude/settings.json is not valid JSON ({exc.msg})"
        raise ValueError(msg) from exc
    except UnicodeDecodeError as exc:
        msg = ".claude/settings.json is not valid UTF-8"
        raise ValueError(msg) from exc
    if not isinstance(data, dict):
        msg = ".claude/settings.json is not a JSON object"
        raise ValueError(msg)
    return cast("dict[str, object]", data), stat.S_IMODE(info.st_mode)


def _allow_list(settings: dict[str, object]) -> list[object]:
    """Return ``permissions.allow``, creating it if absent; refuse bad shapes.

    A ``permissions`` or ``allow`` of the wrong type is the user's data, so it
    is reported rather than overwritten.
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


def _write(dir_fd: int, text: str, mode: int) -> None:
    """Replace ``settings.json`` atomically inside the pinned ``.claude``."""
    tmp = f".settings.json.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW
    fd = os.open(tmp, flags, 0o600, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(tmp, _SETTINGS, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise
