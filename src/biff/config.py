"""Configuration discovery and loading.

Reads YAML config from ``.punt-labs/biff/`` (shared + local override)
or runs in zero-config mode with defaults derived from the git remote.

Data directory layout::

    {prefix}/biff/{directory-name}/
        userinbox-kai.jsonl        # per-user mailbox (broadcast)
        inbox-kai-a1b2c3d4.jsonl   # per-TTY mailbox (targeted)
        userinbox-eric.jsonl
        inbox-eric-12345678.jsonl
        sessions.json
"""

from __future__ import annotations

import getpass
import importlib.resources
import json
import logging
import os
import re
import stat
import subprocess
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import cast

import yaml

from biff._stdlib import (
    enabled_marker_path,
    find_git_root,
    get_repo_common_root,
    get_repo_owner,
    get_repo_slug,
    is_enabled,
    remove_enabled_marker,
    sanitize_repo_name,
    write_enabled_marker,
    yaml_config_dir,
)
from biff.models import BiffConfig, RelayAuth

# Re-export stdlib functions so existing callers of biff.config still work.
__all__ = [
    "enabled_marker_path",
    "find_git_root",
    "get_repo_slug",
    "is_enabled",
    "remove_enabled_marker",
    "sanitize_repo_name",
    "write_enabled_marker",
]

logger = logging.getLogger(__name__)

_DEFAULT_PREFIX = Path("/tmp")  # noqa: S108
DEMO_RELAY_URL = "tls://connect.ngs.global"

# Agent handle grammar — matches identity YAML filenames. Length 1-64, lowercase
# letters, digits, underscore, hyphen; first character cannot be a hyphen or
# underscore. Repo-controlled input, so this is a hard precondition (spec § 3
# step 3, invariant 10) guarding against path-traversal payloads in the
# ``agent`` field of ``.punt-labs/ethos.yaml``.
_AGENT_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def demo_creds_path() -> Path:
    """Resolve the bundled demo credentials file path."""
    return Path(str(importlib.resources.files("biff.data").joinpath("demo.creds")))


@dataclass(frozen=True)
class ResolvedConfig:
    """Fully resolved configuration ready for server startup."""

    config: BiffConfig
    data_dir: Path
    # Parent of ``git rev-parse --git-common-dir``: the same absolute path from
    # the main checkout and every linked worktree.  Distinct from *repo_root*
    # so per-worktree write-through (config yaml, enabled marker) keeps the
    # nearest-worktree semantics.
    # Required: ``_load_base_config`` always resolves it (falling back to
    # ``repo_root`` when ``get_repo_common_root`` returns ``""``), so the
    # optional-with-``None`` shape had no reachable ``None`` branch and
    # forced every reader into a dead ``else ""`` guard.
    repo_common_root: Path
    repo_root: Path | None = None


@dataclass(frozen=True)
class GitHubIdentity:
    """GitHub login and display name resolved from ``gh api user``."""

    login: str
    display_name: str


def get_github_identity() -> GitHubIdentity | None:
    """Resolve GitHub login and display name in a single API call.

    Returns ``None`` when ``gh`` is missing or the call fails.
    """
    try:
        result = subprocess.run(
            [  # noqa: S607
                "gh",
                "api",
                "user",
                "--jq",
                'select(.login) | [.login, .name // ""] | @tsv',
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            # debug, not warning: the common case on a machine without
            # ``gh`` configured is indistinguishable here from an expired
            # token, rate limit, or network error -- avoid noise while
            # still leaving a trail for the uncommon cases.
            logger.debug(
                "gh api user failed (exit %d): %s",
                result.returncode,
                result.stderr.strip(),
            )
            return None
        parts = result.stdout.strip().split("\t", maxsplit=1)
        login = parts[0].strip()
        if not login:
            return None
        display_name = parts[1].strip() if len(parts) > 1 else ""
        return GitHubIdentity(login=login, display_name=display_name)
    except FileNotFoundError:
        return None


@dataclass(frozen=True)
class EthosIdentity:
    """Identity resolved from ``ethos whoami --json``."""

    handle: str
    display_name: str
    kind: str  # "human", "agent", or ""


@dataclass(frozen=True)
class EthosRoster:
    """Session roster from ``ethos session roster --json``."""

    root: EthosIdentity | None
    primary: EthosIdentity | None


def _parse_roster_entry(data: dict[str, object]) -> EthosIdentity | None:
    """Parse a single roster participant into an EthosIdentity.

    Supports two formats:
    - Legacy: ``{"handle": "...", "display_name": "...", "kind": "..."}``
    - Current: ``{"agent_id": "...", "persona": "..."}``
    """
    # Current format: agent_id + persona
    handle = data.get("persona", "") or data.get("handle", "")
    if not isinstance(handle, str) or not handle:
        return None
    name = data.get("display_name", "")
    display_name = name if isinstance(name, str) and name else handle
    kind_val = data.get("kind", "")
    kind = kind_val if isinstance(kind_val, str) else ""
    return EthosIdentity(handle=handle, display_name=display_name, kind=kind)


def _parse_roster_participants(
    participants: list[object],
) -> EthosRoster:
    """Parse roster from the ``participants`` array format."""
    root: EthosIdentity | None = None
    primary: EthosIdentity | None = None
    for p in participants:
        if not isinstance(p, dict):
            continue
        entry = cast("dict[str, object]", p)
        identity = _parse_roster_entry(entry)
        if identity is None:
            continue
        if entry.get("parent"):
            primary = identity
        elif root is None:
            root = identity
    return EthosRoster(root=root, primary=primary)


def _parse_roster_legacy(raw: dict[str, object]) -> EthosRoster:
    """Parse roster from the legacy ``root`` + ``primary`` format."""
    root_raw = raw.get("root")
    primary_raw = raw.get("primary")
    root = (
        _parse_roster_entry(cast("dict[str, object]", root_raw))
        if isinstance(root_raw, dict)
        else None
    )
    primary = (
        _parse_roster_entry(cast("dict[str, object]", primary_raw))
        if isinstance(primary_raw, dict)
        else None
    )
    return EthosRoster(root=root, primary=primary)


def get_ethos_roster() -> EthosRoster | None:
    """Resolve the session roster from ethos CLI.

    Returns ``None`` when ethos is not installed, not configured,
    returns malformed JSON, or times out.
    """
    try:
        result = subprocess.run(
            ["ethos", "session", "roster", "--json"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    raw = cast("dict[str, object]", data)
    participants = raw.get("participants")
    if isinstance(participants, list):
        return _parse_roster_participants(cast("list[object]", participants))
    return _parse_roster_legacy(raw)


@dataclass(frozen=True, slots=True)
class _Unreadable:
    """Sentinel: a path exists but its contents could not be recovered.

    Carries the triggering exception so each caller can log with its own
    context-specific wording -- what a permission-denied or corrupt-encoding
    read *means* differs by caller (a malformed identity file vs. an
    unverifiable ``.gitmodules``), even though the failure mode reaching
    this point is identical.
    """

    exc: OSError | UnicodeDecodeError


def _read_text_or_fail_closed(path: Path) -> str | _Unreadable | None:
    """Return *path*'s UTF-8 text, distinguishing absence from unreadability.

    Three outcomes, not two. Collapsing "confirmed absent"
    (``FileNotFoundError``) and "exists but unreadable" (any other
    ``OSError``, or ``UnicodeDecodeError`` on invalid UTF-8) into a single
    ``None`` is the exact bug this helper exists to make structurally hard
    to repeat -- see DES-053's amendment history in ``DESIGN.md``, where
    that collapse was fixed once in :func:`_read_identity_yaml` and then
    reintroduced from scratch in what is now :func:`_ethos_submodule_declared`.

    Returns ``None`` only for ``FileNotFoundError`` -- the one case safe to
    treat as "nothing to see here." Returns :class:`_Unreadable` for every
    other read failure, so the caller fails closed instead of silently
    treating an unverifiable file as absent.
    """
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        return _Unreadable(exc)


def _read_identity_yaml(path: Path) -> dict[str, object] | None:
    """Load identity YAML, swallowing parse errors with a warning.

    Unlike ``load_yaml_config`` (which raises ``SystemExit`` on parse
    errors), agent identity resolution must never prevent biff from
    starting -- the fallback chain handles missing or malformed
    identity files (spec invariant 8).
    """
    text = _read_text_or_fail_closed(path)
    if text is None:
        # Benign TOCTOU race with the directory listing in
        # _list_identity_yaml_files -- the file existed when listed,
        # gone by the time it's read. Not worth a warning.
        return None
    if isinstance(text, _Unreadable):
        if isinstance(text.exc, UnicodeDecodeError):
            logger.warning("Identity YAML %s is not valid UTF-8: %s", path, text.exc)
        else:
            logger.warning("Failed to read identity YAML %s: %s", path, text.exc)
        return None
    try:
        raw: object = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        logger.warning("Failed to parse identity YAML %s: %s", path, exc)
        return None
    if isinstance(raw, dict):
        return cast("dict[str, object]", raw)
    logger.warning(
        "Identity YAML %s did not parse to a mapping (got %s)",
        path,
        type(raw).__name__,
    )
    return None


@dataclass(frozen=True, slots=True)
class _AgentLoginScan:
    """Result of scanning ``.punt-labs/ethos/identities/`` for bot logins.

    ``complete`` is ``False`` whenever any identity file could not be
    read or parsed. A caller that finds its resolved login absent from
    ``logins`` cannot tell "genuinely not a bot" from "the file that
    would have said so was unreadable" unless it also checks
    ``complete`` -- treating an incomplete scan as trustworthy is
    exactly how a bot's GitHub login leaks into a human identity with
    its own registration file sitting right there, just unreadable.

    ``logins`` entries are casefolded and stripped -- GitHub logins are
    case-insensitive, so callers must normalize the login they compare
    against the same way (see :func:`_resolve_human_identity`).
    """

    logins: frozenset[str]
    complete: bool


def _list_identity_yaml_files(identities_dir: Path) -> tuple[Path, ...] | None:
    """Return every ``*.yaml`` file directly under *identities_dir*.

    Returns ``None`` (already logged) when the directory exists but isn't
    a directory or can't be listed (``OSError``, e.g. permission denied).
    ``None`` here means "this scan cannot be trusted."

    Returns ``()`` when the directory is simply absent
    (``FileNotFoundError``). Absence alone is ambiguous, not
    trustworthy: it looks identical whether this repo has no ethos
    submodule at all, or the ethos submodule is declared in
    ``.gitmodules`` but was never checked out -- ``git clone`` without
    ``--recurse-submodules``, or ``git worktree add`` (which never
    initializes submodules), are the common real-world ways the latter
    happens (see the org CLAUDE.md "Initial checkout" section). This
    function has no way to tell those two cases apart from
    ``identities_dir`` alone; callers that need to must additionally
    consult ``.gitmodules`` (see :func:`_ethos_submodule_declared`) --
    an empty tuple here is not, by itself, license to trust the scan.

    Uses ``iterdir()``, not ``glob()``: ``Path.glob`` silently skips
    directories it can't scandir (it swallows ``PermissionError``
    internally to mimic shell glob semantics), which would fold
    "unreadable" back into "empty" -- exactly the silent failure this
    function exists to surface. ``os.stat`` (unlike ``Path.is_dir`` /
    ``exists``) likewise lets ``PermissionError`` propagate instead of
    folding it into a bare ``False``, distinguishing "directory absent"
    (``FileNotFoundError`` -> ``()``) from "directory present but not
    readable" (``OSError`` -> ``None``).
    """
    try:
        st = identities_dir.stat()
    except FileNotFoundError:
        logger.debug("No identities directory at %s", identities_dir)
        return ()
    except OSError as exc:
        logger.warning(
            "Identities directory %s is not readable: %s", identities_dir, exc
        )
        return None
    if not stat.S_ISDIR(st.st_mode):
        logger.warning("%s exists but is not a directory", identities_dir)
        return None
    try:
        return tuple(sorted(p for p in identities_dir.iterdir() if p.suffix == ".yaml"))
    except OSError as exc:
        logger.warning(
            "Identities directory %s is not readable: %s", identities_dir, exc
        )
        return None


def _scan_agent_logins(paths: tuple[Path, ...]) -> _AgentLoginScan:
    """Read every identity file in *paths*, collecting agent GitHub logins.

    Counts entries this scan could not fully account for: unreadable/
    unparsable files, and ``kind: agent`` entries that DO declare a
    ``github`` field but with an unusable value. A ``github`` field
    that's simply absent is the ordinary, expected shape for most agent
    identities (most agents aren't registered bot accounts) and is not
    counted here -- only a field that's present but broken (null, wrong
    type, empty string) is evidence something is wrong.
    """
    logins: set[str] = set()
    incomplete = 0
    for path in paths:
        identity = _read_identity_yaml(path)
        if identity is None:
            # _read_identity_yaml already logged the specific OSError /
            # UnicodeDecodeError / YAMLError.
            incomplete += 1
            continue
        if identity.get("kind") != "agent" or "github" not in identity:
            continue
        github = identity.get("github")
        if isinstance(github, str) and github.strip():
            # GitHub logins are case-insensitive; casefold + strip so a
            # registry entry that differs only by case or trailing
            # whitespace from `gh api user`'s canonical `.login` still
            # lands in the denylist (see the matching normalization in
            # _resolve_human_identity).
            logins.add(github.strip().casefold())
        else:
            incomplete += 1
            logger.warning(
                "Agent identity %s has a malformed 'github' field (got %r); "
                "it cannot be cross-checked against a resolved GitHub login",
                path,
                github,
            )
    if incomplete:
        logger.warning(
            "Scanned %d identity files, %d incomplete or unreadable",
            len(paths),
            incomplete,
        )
    return _AgentLoginScan(logins=frozenset(logins), complete=incomplete == 0)


_ETHOS_SUBMODULE_PATH = ".punt-labs/ethos"


class _SubmoduleDeclaration(Enum):
    """Whether ``.gitmodules`` declares a submodule at ``.punt-labs/ethos``.

    Three states, not two -- ``ABSENT`` and ``UNVERIFIABLE`` must stay
    distinguishable all the way to the caller. Only ``ABSENT`` is safe to
    treat as "no ethos integration here." Both ``DECLARED`` and
    ``UNVERIFIABLE`` mean the caller cannot trust an empty identities scan
    and must fail closed, for different but equally load-bearing reasons:
    one is a confirmed match, the other is a match that couldn't be ruled
    out.
    """

    ABSENT = "absent"
    DECLARED = "declared"
    UNVERIFIABLE = "unverifiable"


def _ethos_submodule_declared(repo_root: Path) -> _SubmoduleDeclaration:
    """Classify whether ``.gitmodules`` declares a submodule at ``.punt-labs/ethos``.

    A missing ``.punt-labs/ethos/identities/`` directory is ambiguous on
    its own: it looks identical whether this repo has no ethos
    integration at all (genuinely empty, safe to trust) or the ethos
    submodule is declared but was never checked out (unverifiable, must
    fail closed per DES-053). ``.gitmodules`` is an ordinary git-tracked
    file present in every checkout and worktree regardless of
    submodule-init state, so its content -- not directory presence -- is
    the reliable signal for which case applies. A simple text search for
    the ``path =`` line is sufficient here; ``.gitmodules`` is trusted
    repo content, not adversarial input, so a full INI parser buys
    nothing.

    An unreadable or non-UTF-8 ``.gitmodules`` is classified
    ``UNVERIFIABLE``, not ``ABSENT`` -- a ``.gitmodules`` that correctly
    declares the submodule but happens to be unreadable must fail closed
    exactly like a confirmed ``DECLARED`` match, or the bot-impersonation
    vulnerability DES-053 exists to close reopens silently and without a
    log line. Routes through :func:`_read_text_or_fail_closed` so this
    distinction doesn't have to be reimplemented by hand here.
    """
    gitmodules = repo_root / ".gitmodules"
    text = _read_text_or_fail_closed(gitmodules)
    if text is None:
        return _SubmoduleDeclaration.ABSENT
    if isinstance(text, _Unreadable):
        if isinstance(text.exc, UnicodeDecodeError):
            logger.warning(
                "%s is not valid UTF-8 -- treating the ethos submodule "
                "declaration as unverifiable: %s",
                gitmodules,
                text.exc,
            )
        else:
            logger.warning(
                "%s exists but could not be read -- treating the ethos "
                "submodule declaration as unverifiable: %s",
                gitmodules,
                text.exc,
            )
        return _SubmoduleDeclaration.UNVERIFIABLE
    # ``\r?`` before the end anchor: a .gitmodules checked out with CRLF
    # line endings leaves a trailing \r that [ \t]*$ alone would not
    # consume, producing a false ABSENT for a submodule that is in fact
    # declared.
    pattern = rf"^[ \t]*path[ \t]*=[ \t]*{re.escape(_ETHOS_SUBMODULE_PATH)}[ \t]*\r?$"
    if re.search(pattern, text, re.MULTILINE) is not None:
        return _SubmoduleDeclaration.DECLARED
    return _SubmoduleDeclaration.ABSENT


def _known_agent_github_logins(repo_root: Path) -> _AgentLoginScan:
    """Return the GitHub logins of every ``kind: agent`` identity on disk.

    Scans ``{repo_root}/.punt-labs/ethos/identities/*.yaml`` directly --
    unlike :func:`resolve_agent_identity_from_disk`, this does not
    require ``ethos.yaml`` to name a specific agent. A durable Claude
    Agento shell sources a bot's ``GH_TOKEN`` for its entire lifetime
    (org CLAUDE.md), so ``gh api user`` can resolve to the bot even when
    a human is at the keyboard. Cross-checking the resolved login
    against the repo's own identity registry lets
    :func:`_resolve_human_identity` detect and reject that case.

    Returns an empty, complete scan when the identities directory is
    absent or empty AND ``.gitmodules`` doesn't declare an ethos
    submodule -- inert in repos with no ethos integration at all.
    Returns an empty, *incomplete* scan when the directory is absent or
    empty but ``.gitmodules`` DOES declare a submodule at
    ``.punt-labs/ethos``, or when ``.gitmodules`` itself couldn't be read
    or decoded: an uninitialized submodule is unverifiable, and so is a
    submodule declaration we can't confirm one way or the other -- neither
    is "confirmed no bots" (see :func:`_ethos_submodule_declared`,
    DES-053).
    """
    identities_dir = repo_root / ".punt-labs" / "ethos" / "identities"
    paths = _list_identity_yaml_files(identities_dir)
    if paths is None:
        return _AgentLoginScan(logins=frozenset(), complete=False)
    if not paths:
        declaration = _ethos_submodule_declared(repo_root)
        if declaration is _SubmoduleDeclaration.DECLARED:
            logger.warning(
                "%s declares a submodule at %s but %s has no identity files -- "
                "treating the bot-login scan as incomplete (uninitialized "
                "submodule, not 'no ethos integration'); run "
                "`git submodule update --init` to populate it",
                repo_root / ".gitmodules",
                _ETHOS_SUBMODULE_PATH,
                identities_dir,
            )
            return _AgentLoginScan(logins=frozenset(), complete=False)
        if declaration is _SubmoduleDeclaration.UNVERIFIABLE:
            # _ethos_submodule_declared already logged the specific
            # OSError / UnicodeDecodeError.
            return _AgentLoginScan(logins=frozenset(), complete=False)
    return _scan_agent_logins(paths)


def _find_ethos_config(repo_root: Path) -> Path | None:
    """Return the ethos config path, preferring the new location over the legacy one."""
    primary = repo_root / ".punt-labs" / "ethos.yaml"
    if primary.exists():
        return primary
    legacy = repo_root / ".punt-labs" / "ethos" / "config.yaml"
    return legacy if legacy.exists() else None


def _read_agent_handle(ethos_config: Path) -> str | None:
    """Read and validate the ``agent`` field from ``ethos.yaml``.

    Returns the validated handle, or ``None`` on missing/invalid input.
    The handle is gated by ``_AGENT_HANDLE_RE`` because
    ``.punt-labs/ethos.yaml`` is repository content -- a malicious or
    mistyped value (``../../etc/passwd``, ``foo/bar``) must never be
    joined onto a filesystem path.
    """
    config = _read_identity_yaml(ethos_config)
    if not config:
        return None
    agent_raw = config.get("agent")
    if not isinstance(agent_raw, str):
        return None
    agent = agent_raw.strip()
    if not agent:
        return None
    if not _AGENT_HANDLE_RE.match(agent):
        logger.warning(
            "Rejecting agent handle %r in %s: must match %s",
            agent,
            ethos_config,
            _AGENT_HANDLE_RE.pattern,
        )
        return None
    return agent


def _resolve_identity_path(repo_root: Path, handle: str) -> Path | None:
    """Resolve the identity YAML path, guarding against traversal.

    Defense in depth: even if the regex misses a payload (locale
    normalization, future grammar changes), the resolved path must
    stay within ``{repo_root}/.punt-labs/ethos/identities/``.
    """
    identities_root = (repo_root / ".punt-labs" / "ethos" / "identities").resolve()
    identity_path = (identities_root / f"{handle}.yaml").resolve()
    if not identity_path.is_relative_to(identities_root):
        logger.warning(
            "Rejecting identity path %s: outside %s",
            identity_path,
            identities_root,
        )
        return None
    return identity_path if identity_path.exists() else None


def _build_agent_identity(identity_path: Path, agent: str) -> EthosIdentity | None:
    """Parse the identity YAML and enforce ``kind == "agent"``.

    A repo-controlled ``ethos.yaml`` cannot escalate a human (or any
    non-agent kind) into the agent slot (spec invariant 10).
    """
    identity = _read_identity_yaml(identity_path)
    if not identity:
        return None
    yaml_handle = identity.get("handle", "")
    if isinstance(yaml_handle, str) and yaml_handle and yaml_handle != agent:
        logger.warning(
            "Identity %s declares handle %r; using validated agent handle %r",
            identity_path,
            yaml_handle,
            agent,
        )
    handle = agent  # Always use the validated filename-derived handle
    name_raw = identity.get("name", handle)
    display_name = name_raw if isinstance(name_raw, str) and name_raw else handle
    kind_raw = identity.get("kind", "")
    kind = kind_raw if isinstance(kind_raw, str) else ""
    if kind != "agent":
        logger.warning(
            "Rejecting identity %s: kind=%r (expected 'agent')",
            identity_path,
            kind,
        )
        return None
    return EthosIdentity(handle=handle, display_name=display_name, kind=kind)


def resolve_agent_identity_from_disk(repo_root: Path) -> EthosIdentity | None:
    """Resolve agent identity from ethos config files on disk.

    Reads ``{repo_root}/.punt-labs/ethos.yaml`` for the ``agent`` field,
    then ``{repo_root}/.punt-labs/ethos/identities/{agent}.yaml`` for
    identity details. Returns ``None`` on any failure (missing files,
    parse errors, empty fields, invalid handle grammar, path-traversal
    attempt, or ``kind != "agent"``) -- never raises.
    """
    ethos_config = _find_ethos_config(repo_root)
    if ethos_config is None:
        return None
    agent = _read_agent_handle(ethos_config)
    if agent is None:
        return None
    identity_path = _resolve_identity_path(repo_root, agent)
    if identity_path is None:
        return None
    return _build_agent_identity(identity_path, agent)


def _extract_team_members(teams: list[object]) -> set[str]:
    """Extract the union of member identities from ethos team JSON."""
    members: set[str] = set()
    for team in teams:
        if not isinstance(team, dict):
            continue
        raw_team = cast("dict[str, object]", team)
        raw_members = raw_team.get("members", [])
        if not isinstance(raw_members, list):
            continue
        for member in cast("list[object]", raw_members):
            if isinstance(member, dict):
                identity = cast("dict[str, object]", member).get("identity")
                if isinstance(identity, str) and identity.strip():
                    members.add(identity.strip())
    return members


def get_ethos_team() -> tuple[str, ...] | None:
    """Resolve team members from the ethos CLI.

    Returns a sorted tuple of identity handles, or ``None`` on any
    failure or when the repo is not in any team.
    """
    try:
        result = subprocess.run(
            ["ethos", "team", "for-repo", "--json"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    try:
        teams = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(teams, list) or not teams:
        return None
    all_members = _extract_team_members(cast("list[object]", teams))
    if not all_members:
        return None
    return tuple(sorted(all_members))


def get_os_user() -> str | None:
    """Return the OS username, or ``None`` if unavailable."""
    try:
        return getpass.getuser()
    except OSError:
        return None


def compute_data_dir(repo_root: Path, prefix: Path) -> Path:
    """Compute data directory: ``{prefix}/biff/{repo_root.name}/``."""
    return prefix / "biff" / repo_root.name


# ── YAML config pipeline ───────────────────────────────────────────


def _load_yaml(path: Path) -> dict[str, object]:
    """Load a YAML file and return a dict, or ``{}`` on error.

    Catches ``OSError`` (permissions, TOCTOU race) and
    ``UnicodeDecodeError`` (invalid text encoding), returning ``{}``.
    Lets ``yaml.YAMLError`` propagate so callers can decide severity.
    """
    try:
        raw: object = yaml.safe_load(path.read_text())
    except (OSError, UnicodeDecodeError):
        return {}
    if isinstance(raw, dict):
        return cast("dict[str, object]", raw)
    return {}


def load_yaml_config(repo_root: Path) -> dict[str, object]:
    """Read ``.punt-labs/biff/config.yaml``, return dict or ``{}``."""
    path = yaml_config_dir(repo_root) / "config.yaml"
    if not path.exists():
        return {}
    try:
        return _load_yaml(path)
    except yaml.YAMLError as exc:
        raise SystemExit(
            f"Failed to parse {path}:\n{exc}\n"
            "Fix or remove this file before starting biff."
        ) from exc


def load_yaml_local(repo_root: Path) -> dict[str, object]:
    """Read ``.punt-labs/biff/config.local.yaml``, return dict or ``{}``."""
    path = yaml_config_dir(repo_root) / "config.local.yaml"
    if not path.exists():
        return {}
    try:
        return _load_yaml(path)
    except yaml.YAMLError:
        return {}


def _deep_merge(
    base: dict[str, object], override: dict[str, object]
) -> dict[str, object]:
    """Deep merge *override* into *base*, returning a new dict.

    At each level, dict values are merged recursively; all other
    types are replaced wholesale by the override value.
    """
    merged: dict[str, object] = {**base}
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(
                cast("dict[str, object]", merged[key]),
                cast("dict[str, object]", value),
            )
        else:
            merged[key] = value
    return merged


def merge_config(
    shared: dict[str, object], local: dict[str, object]
) -> dict[str, object]:
    """Deep merge local overrides on top of shared config."""
    return _deep_merge(shared, local)


def write_yaml_config(
    repo_root: Path, data: dict[str, object], *, local: bool = False
) -> Path:
    """Atomically write YAML config to ``.punt-labs/biff/``.

    When *local* is ``True``, writes ``config.local.yaml``;
    otherwise writes ``config.yaml``.  Returns the written path.
    """
    from biff.relay import atomic_write  # noqa: PLC0415

    config_dir = yaml_config_dir(repo_root)
    config_dir.mkdir(parents=True, exist_ok=True)
    filename = "config.local.yaml" if local else "config.yaml"
    path = config_dir / filename
    content = yaml.safe_dump(data, default_flow_style=False, sort_keys=False)
    atomic_write(path, content)
    return path


def ensure_gitignore_yaml(repo_root: Path) -> None:
    """Add ``config.local.yaml`` to ``.punt-labs/biff/.gitignore``."""
    config_dir = yaml_config_dir(repo_root)
    config_dir.mkdir(parents=True, exist_ok=True)
    gitignore = config_dir / ".gitignore"
    entry = "config.local.yaml"
    if gitignore.exists():
        content = gitignore.read_text()
        if any(line.strip() == entry for line in content.splitlines()):
            return
        if not content.endswith("\n"):
            content += "\n"
        content += entry + "\n"
        gitignore.write_text(content)
    else:
        gitignore.write_text(entry + "\n")


# ── Field extraction ───────────────────────────────────────────────


def _extract_peers(
    raw: dict[str, object],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Extract peers and orgs from the ``peers`` section."""
    peers: tuple[str, ...] = ()
    orgs: tuple[str, ...] = ()
    peers_section: object = raw.get("peers")
    if not isinstance(peers_section, dict):
        return peers, orgs
    section = cast("dict[str, object]", peers_section)
    repos: object = section.get("repos", [])
    if isinstance(repos, list):
        items_p = cast("list[object]", repos)
        peers = tuple(
            sanitize_repo_name(r) for r in items_p if isinstance(r, str) and r
        )
    # Org names are sanitized for NATS subject safety.
    # The relay appends "__>" for the subjects_filter query.
    orgs_raw: object = section.get("orgs", [])
    if isinstance(orgs_raw, list):
        items_o = cast("list[object]", orgs_raw)
        orgs = tuple(sanitize_repo_name(o) for o in items_o if isinstance(o, str) and o)
    return peers, orgs


def _extract_relay(
    raw: dict[str, object],
) -> tuple[str | None, RelayAuth | None, bool]:
    """Extract relay URL, auth, and TLS handshake mode from the ``relay`` section."""
    relay_section: object = raw.get("relay")
    if not isinstance(relay_section, dict):
        return None, None, False

    section = cast("dict[str, object]", relay_section)
    url: object = section.get("url")
    relay_url = url if isinstance(url, str) else None
    tls_handshake_first = section.get("tls_handshake_first") is True

    # Auth -- at most one of token, nkeys_seed, user_credentials.
    # TOML uses flat keys; YAML uses nested ``auth:`` mapping.
    token = section.get("token")
    nkeys_seed = section.get("nkeys_seed")
    creds = section.get("user_credentials")
    auth_section: object = section.get("auth")
    if isinstance(auth_section, dict):
        auth_d = cast("dict[str, object]", auth_section)
        if token is None:
            token = auth_d.get("token")
        if nkeys_seed is None:
            nkeys_seed = auth_d.get("nkeys_seed")
        if creds is None:
            creds = auth_d.get("credentials") or auth_d.get("user_credentials")

    auth_values = {
        k: v
        for k, v in [
            ("token", token),
            ("nkeys_seed", nkeys_seed),
            ("user_credentials", creds),
        ]
        if isinstance(v, str) and v
    }
    if len(auth_values) > 1:
        names = ", ".join(sorted(auth_values))
        raise SystemExit(
            f"Conflicting auth in relay section: {names}\n"
            "Set at most one of 'token', 'nkeys_seed', "
            "or 'user_credentials'."
        )
    relay_auth = RelayAuth(**auth_values) if auth_values else None

    # Default to bundled demo credentials for the demo relay
    if relay_url == DEMO_RELAY_URL and relay_auth is None:
        relay_auth = RelayAuth(user_credentials=str(demo_creds_path()))

    return relay_url, relay_auth, tls_handshake_first


def _extract_team(raw: dict[str, object]) -> tuple[str, ...]:
    """Extract team members from the ``team`` section."""
    team_section: object = raw.get("team")
    if not isinstance(team_section, dict):
        return ()
    section = cast("dict[str, object]", team_section)
    members: object = section.get("members", [])
    if not isinstance(members, list):
        return ()
    items = cast("list[object]", members)
    return tuple(m for m in items if isinstance(m, str))


def _extract_poll_interval(raw: dict[str, object]) -> float:
    """Extract ``poll_interval`` from the config dict.

    Accepts top-level ``poll_interval`` key.  Returns the default
    (2.0s) when absent or invalid.  ``0`` means disabled (set by
    ``set_poll_interval("n")``).
    """
    value: object = raw.get("poll_interval")
    if isinstance(value, (int, float)) and value >= 0:
        return float(value)
    return 2.0


def extract_biff_fields(
    raw: dict[str, object],
) -> tuple[
    tuple[str, ...],
    str | None,
    RelayAuth | None,
    bool,
    tuple[str, ...],
    tuple[str, ...],
]:
    """Extract team, relay_url, relay_auth, relay_tls_handshake_first, peers, orgs."""
    team = _extract_team(raw)
    relay_url, relay_auth, tls_handshake_first = _extract_relay(raw)
    peers, orgs = _extract_peers(raw)
    return team, relay_url, relay_auth, tls_handshake_first, peers, orgs


RELAY_URL_UNSET = object()


@dataclass(frozen=True)
class _ConfigFields:
    """Intermediate container for fields resolved from config files."""

    team: tuple[str, ...] = ()
    relay_url: str | None = None
    relay_auth: RelayAuth | None = None
    relay_tls_handshake_first: bool = False
    peers: tuple[str, ...] = ()
    orgs: tuple[str, ...] = ()
    poll_interval: float = 2.0
    # Defense-in-depth gate for _apply_env_relay_overrides (docs/relay-env
    # -overrides.md Sec 0): a repo that already commits its own relay.url
    # must opt in explicitly before an ambient BIFF_RELAY_* env var can
    # override it. Defaults False -- silence means "no," not "maybe."
    relay_allow_env_override: bool = False


def _has_orgs_key(raw: dict[str, object]) -> bool:
    """Check if peers.orgs is explicitly set in the config dict.

    Distinguishes "key absent" from "key present but empty list" so
    users can configure ``peers.orgs: []`` to disable org discovery.
    """
    peers = raw.get("peers")
    return isinstance(peers, dict) and "orgs" in peers


def _relay_allow_env_override(raw: dict[str, object]) -> bool:
    """Check if the merged config explicitly opts into env-var relay overrides.

    Absent or any non-``True`` value means "no" -- this gate defaults
    closed (see :class:`_ConfigFields`).
    """
    relay_section = raw.get("relay")
    if not isinstance(relay_section, dict):
        return False
    section = cast("dict[str, object]", relay_section)
    return section.get("allow_env_override") is True


def _enrich_team(cf: _ConfigFields) -> _ConfigFields:
    """Enrich team from ethos when no explicit team is configured."""
    if cf.team:
        return cf
    ethos_team = get_ethos_team()
    if ethos_team is None:
        return cf
    return replace(cf, team=ethos_team)


def _resolve_config_fields(repo_root: Path) -> _ConfigFields:
    """Resolve config fields from YAML or zero-config.

    Detection order:

    1. ``.punt-labs/biff/config.yaml`` -- explicit mode.
    2. Neither -- zero-config with derived defaults.
    """
    # Key explicit mode on file existence, not content truthiness.
    # An empty or comment-only config.yaml should still mean "explicit
    # mode" — not silently fall through to zero-config derivation.
    shared_path = yaml_config_dir(repo_root) / "config.yaml"
    if shared_path.exists():
        yaml_shared = load_yaml_config(repo_root)
        yaml_local = load_yaml_local(repo_root)
        merged = merge_config(yaml_shared, yaml_local)
        fields = extract_biff_fields(merged)
        poll_interval = _extract_poll_interval(merged)
        cf = _ConfigFields(*fields, poll_interval=poll_interval)
        cf = replace(cf, relay_allow_env_override=_relay_allow_env_override(merged))
        # Derive orgs from remote only when the peers.orgs key is
        # ABSENT from the merged config. An explicit empty list
        # (peers.orgs: []) is honored — it means "no org discovery."
        if not _has_orgs_key(merged):
            owner = get_repo_owner(repo_root)
            cf = replace(cf, orgs=(owner,) if owner else ())
        return _enrich_team(cf)

    # Zero-config: derive org from remote, use demo relay.
    # Still read config.local.yaml — user may have set relay via
    # biff_relay --local without a shared config.yaml.
    yaml_local = load_yaml_local(repo_root)
    if yaml_local:
        fields = extract_biff_fields(yaml_local)
        poll_interval = _extract_poll_interval(yaml_local)
        cf = _ConfigFields(*fields, poll_interval=poll_interval)
        # Derive owner only when peers.orgs key is absent.
        if _has_orgs_key(yaml_local):
            orgs = cf.orgs
        else:
            owner = get_repo_owner(repo_root)
            orgs = (owner,) if owner else ()
        # Leave relay_url/relay_auth as resolved from config.local.yaml
        # (which may be None) -- _apply_demo_relay_default, called after
        # _apply_env_relay_overrides in _load_base_config, is the single
        # place that fills in the demo relay fallback.
        return _enrich_team(replace(cf, orgs=orgs))

    owner = get_repo_owner(repo_root)
    orgs = (owner,) if owner else ()
    # Leave relay_url/relay_auth unset here -- _apply_demo_relay_default
    # (called from _load_base_config, after _apply_env_relay_overrides) is
    # the single place that fills in the demo relay fallback. Applying it
    # eagerly here, before the env-override gate runs, would make every
    # truly zero-config repo look identical to a repo that has committed
    # its own relay.url, silently closing the gate that Sec 0 requires to
    # stay open in exactly this case.
    return _enrich_team(_ConfigFields(orgs=orgs))


def _apply_demo_relay_default(
    relay_url: str | None, relay_auth: RelayAuth | None
) -> tuple[str, RelayAuth | None]:
    """Ensure demo relay is the fallback when no relay is specified."""
    if relay_url is None:
        relay_url = DEMO_RELAY_URL
    if relay_url == DEMO_RELAY_URL and relay_auth is None:
        relay_auth = RelayAuth(user_credentials=str(demo_creds_path()))
    return relay_url, relay_auth


_TLS_TRUE_VALUES = frozenset({"1", "true", "yes"})

_AUTH_ENV_FIELDS: dict[str, str] = {
    "BIFF_RELAY_TOKEN": "token",
    "BIFF_RELAY_NKEYS_SEED": "nkeys_seed",
    "BIFF_RELAY_USER_CREDENTIALS": "user_credentials",
}

# The two auth env vars that name a file on disk -- fail fast if the file
# is missing rather than let it surface later as an opaque NATS connect
# error (docs/relay-env-overrides.md, operator ruling on open question 2).
_AUTH_ENV_PATH_FIELDS = frozenset(
    {"BIFF_RELAY_NKEYS_SEED", "BIFF_RELAY_USER_CREDENTIALS"}
)


def _env_or_none(name: str) -> str | None:
    """Read an environment variable, treating an empty string as absent.

    Mirrors how ``config.py`` already treats absent-vs-empty elsewhere
    (e.g. :func:`_has_orgs_key`) -- an unset repository *variable* in a
    workflow's ``env:`` block expands to ``""``, which must behave
    identically to the var never having been set at all.
    """
    return os.environ.get(name, "") or None


def _apply_env_relay_overrides(cf: _ConfigFields) -> _ConfigFields:
    """Layer ``BIFF_RELAY_*`` env vars over file-resolved relay fields.

    Precedence: ``config.yaml`` < ``config.local.yaml`` < env vars < the
    CLI ``--relay-url`` override applied later in :func:`_load_base_config`
    (docs/relay-env-overrides.md Sec 1). No-ops unless the repo-scoping
    gate (Sec 0) is satisfied: the file-resolved relay URL is unset, or
    the repo's config explicitly opts in via
    ``relay.allow_env_override: true``.

    Raises :class:`SystemExit` when two or more of ``BIFF_RELAY_TOKEN``,
    ``BIFF_RELAY_NKEYS_SEED``, and ``BIFF_RELAY_USER_CREDENTIALS`` are set
    simultaneously (naming the conflicting variables, never their values),
    or when ``BIFF_RELAY_NKEYS_SEED``/``BIFF_RELAY_USER_CREDENTIALS`` names
    a path that does not exist.
    """
    if cf.relay_url is not None and not cf.relay_allow_env_override:
        return cf

    auth_env = {name: _env_or_none(name) for name in _AUTH_ENV_FIELDS}
    fired_auth = [name for name, value in auth_env.items() if value is not None]
    if len(fired_auth) > 1:
        names = ", ".join(sorted(fired_auth))
        raise SystemExit(
            f"Conflicting relay auth env vars: {names}\n"
            "Set at most one of BIFF_RELAY_TOKEN, BIFF_RELAY_NKEYS_SEED, "
            "or BIFF_RELAY_USER_CREDENTIALS."
        )

    fired: list[str] = []
    relay_url = cf.relay_url
    relay_auth = cf.relay_auth
    tls_handshake_first = cf.relay_tls_handshake_first

    if fired_auth:
        (auth_var,) = fired_auth
        path_value = auth_env[auth_var]
        if (
            auth_var in _AUTH_ENV_PATH_FIELDS
            and not Path(cast("str", path_value)).exists()
        ):
            raise SystemExit(
                f"{auth_var} points to a file that does not exist: {path_value}"
            )
        # Wholesale replace -- never merge with the file-resolved RelayAuth
        # (Sec 2): a token from a file and an nkeys_seed from the
        # environment must never combine into one RelayAuth instance, which
        # would violate RelayAuth's own single-field invariant.
        relay_auth = RelayAuth(**{_AUTH_ENV_FIELDS[auth_var]: path_value})
        fired.append(auth_var)

    url_env = _env_or_none("BIFF_RELAY_URL")
    if url_env is not None:
        relay_url = url_env
        fired.append("BIFF_RELAY_URL")
        if not fired_auth:
            # URL-changes-clears-auth (Sec 2, direct #383 lineage): auth and
            # TLS mode are properties of the relay being replaced, not
            # portable to whatever BIFF_RELAY_URL now points at.
            relay_auth = None
            tls_handshake_first = False

    tls_env = os.environ.get("BIFF_RELAY_TLS_HANDSHAKE_FIRST", "")
    if tls_env.strip().casefold() in _TLS_TRUE_VALUES:
        tls_handshake_first = True
        fired.append("BIFF_RELAY_TLS_HANDSHAKE_FIRST")

    for name in fired:
        # Log only which env var fired, never its value (Sec 5 item 2) --
        # BIFF_RELAY_TOKEN's value must never reach a log line.
        logger.info("relay override: %s set", name)

    return replace(
        cf,
        relay_url=relay_url,
        relay_auth=relay_auth,
        relay_tls_handshake_first=tls_handshake_first,
    )


_NO_USER_MSG = (
    "No user configured. Install the gh CLI and authenticate, or pass --user <handle>"
)


@dataclass(frozen=True)
class _BaseConfig:
    """Non-identity portion of a resolved config, shared by both entry points."""

    repo_root: Path
    # Parent of ``git rev-parse --git-common-dir``: the same absolute path from
    # the main checkout and every linked worktree.  Distinct from *repo_root*
    # (which stays at the nearest worktree top) so per-worktree write-through
    # (config yaml, ``enabled`` marker) keeps the nearest-worktree semantics
    # while cross-worktree coordination (wall markers, collision detection)
    # collapses linked worktrees into one unit.
    repo_common_root: Path
    repo_name: str
    data_dir: Path
    relay_url: str | None
    relay_auth: RelayAuth | None
    relay_tls_handshake_first: bool
    team: tuple[str, ...]
    peers: tuple[str, ...]
    orgs: tuple[str, ...]
    poll_interval: float


def _load_base_config(
    *,
    data_dir_override: Path | None,
    relay_url_override: object,
    prefix: Path,
    start: Path | None,
) -> _BaseConfig:
    """Resolve everything except identity (repo, relay, team, peers, data dir).

    Raises :class:`SystemExit` when *start* is not inside a git
    repository, the repo directory name fails
    :func:`sanitize_repo_name`, ``config.yaml`` is malformed, the relay
    section contains conflicting auth keys, or ``BIFF_RELAY_*`` env vars
    conflict or name a missing credentials file (see
    :func:`_apply_env_relay_overrides`).
    """
    repo_root = find_git_root(start)
    if repo_root is None:
        raise SystemExit("Not in a git repository. Run biff from inside a repo.")

    common_str = get_repo_common_root(str(repo_root))
    repo_common_root = Path(common_str) if common_str else repo_root

    cf = _resolve_config_fields(repo_root)
    cf = _apply_env_relay_overrides(cf)
    relay_url_resolved, relay_auth = _apply_demo_relay_default(
        cf.relay_url, cf.relay_auth
    )
    relay_url: str | None = relay_url_resolved
    relay_tls_handshake_first = cf.relay_tls_handshake_first

    # CLI relay-url override: empty string -> local relay,
    # non-empty -> use it.  Always clear relay_auth and
    # relay_tls_handshake_first on override -- both are properties of the
    # relay being replaced, not the override target.  Leaving
    # tls_handshake_first set would silently force it onto whatever the
    # override points at (e.g. the demo relay, whose native-TLS
    # nats-server must never get it -- WRONG_VERSION_NUMBER).
    if relay_url_override is not RELAY_URL_UNSET:
        override = str(relay_url_override) if relay_url_override else ""
        relay_url = override or None
        relay_auth = None
        relay_tls_handshake_first = False

    repo_slug = get_repo_slug(repo_root)
    repo_name = sanitize_repo_name(repo_slug or repo_root.name)
    data_dir = (
        data_dir_override
        if data_dir_override is not None
        else compute_data_dir(repo_root, prefix)
    )

    return _BaseConfig(
        repo_root=repo_root,
        repo_common_root=repo_common_root,
        repo_name=repo_name,
        data_dir=data_dir,
        relay_url=relay_url,
        relay_auth=relay_auth,
        relay_tls_handshake_first=relay_tls_handshake_first,
        team=cf.team,
        peers=cf.peers,
        orgs=cf.orgs,
        poll_interval=cf.poll_interval,
    )


_BOT_GITHUB_NO_USER_MSG = (
    "GitHub identity {login!r} is registered as a bot/agent account in "
    ".punt-labs/ethos/identities/; refusing to use it as your CLI identity. "
    "Pass --user <handle>, or run biff from a shell whose GH_TOKEN is not "
    "pinned to a bot."
)

_UNVERIFIED_GITHUB_NO_USER_MSG = (
    "Cannot confirm GitHub identity {login!r} is not a bot/agent account -- "
    "the scan of .punt-labs/ethos/identities/ was incomplete (see the "
    "warnings above), so refusing to use it as your CLI identity out of "
    "caution. Pass --user <handle>, or fix the identity file(s) that "
    "failed to read."
)


def _resolve_human_identity(
    user_override: str | None, repo_root: Path
) -> tuple[str, str, str]:
    """Return (user, display_name, kind) from the human-identity chain.

    Chain: ``user_override`` -> ``get_github_identity()`` -> ``get_os_user()``.
    A resolved GitHub login that matches a ``kind: agent`` identity in
    ``.punt-labs/ethos/identities/`` is rejected and treated the same as
    ``get_github_identity()`` returning ``None`` -- a bot's ``GH_TOKEN``
    pinned into a human's shell (every Claude Agento session sources one,
    see ``~/.punt-labs/git-identity.env``) must never silently become the
    human's biff identity (DES-053).

    The same rejection applies, more cautiously, when the identity scan
    itself is incomplete (an identity file was unreadable or malformed):
    trusting an incomplete scan would let a corrupted registration file
    reopen the exact leak this function exists to close, just silently
    instead of via a missing check.

    Raises :class:`SystemExit` when no identity source succeeds.
    """
    if user_override is not None:
        return user_override, "", ""
    identity = get_github_identity()
    # None means "not applicable" (no login rejected for incompleteness),
    # not "failed to compute" -- only set when the scan-incomplete branch
    # below is taken.
    unverified_login: str | None = None
    if identity is not None:
        scan = _known_agent_github_logins(repo_root)
        # scan.logins is already casefolded + stripped -- normalize this
        # side identically or a same-login-different-case bot fails open.
        if identity.login.strip().casefold() in scan.logins:
            logger.warning(
                "GitHub login %r matches a known bot/agent identity; "
                "falling back to OS user for CLI identity",
                identity.login,
            )
        elif not scan.complete:
            unverified_login = identity.login
            logger.warning(
                "Cannot confirm GitHub login %r is not a bot/agent identity "
                "-- the identity scan was incomplete; falling back to OS "
                "user for CLI identity out of caution",
                identity.login,
            )
        else:
            return identity.login, identity.display_name, ""
    os_user = get_os_user()
    if os_user is None:
        if unverified_login is not None:
            raise SystemExit(
                _UNVERIFIED_GITHUB_NO_USER_MSG.format(login=unverified_login)
            )
        if identity is not None:
            raise SystemExit(_BOT_GITHUB_NO_USER_MSG.format(login=identity.login))
        raise SystemExit(_NO_USER_MSG)
    return os_user, "", ""


def _assemble_config(
    base: _BaseConfig, user: str, display_name: str, kind: str
) -> ResolvedConfig:
    """Build a ``ResolvedConfig`` from a base config and resolved identity."""
    config = BiffConfig(
        user=user,
        display_name=display_name,
        kind=kind,
        repo_name=base.repo_name,
        relay_url=base.relay_url,
        relay_auth=base.relay_auth,
        relay_tls_handshake_first=base.relay_tls_handshake_first,
        team=base.team,
        peers=base.peers,
        orgs=base.orgs,
        poll_interval=base.poll_interval,
    )
    return ResolvedConfig(
        config=config,
        data_dir=base.data_dir,
        repo_root=base.repo_root,
        repo_common_root=base.repo_common_root,
    )


def load_mcp_config(
    *,
    user_override: str | None = None,
    data_dir_override: Path | None = None,
    relay_url_override: object = RELAY_URL_UNSET,
    prefix: Path = _DEFAULT_PREFIX,
    start: Path | None = None,
) -> ResolvedConfig:
    """Discover and resolve configuration for the MCP server.

    Identity chain:
    ``user_override`` -> :func:`resolve_agent_identity_from_disk` ->
    :func:`get_github_identity` -> :func:`get_os_user`.

    The MCP server runs inside the agent's process; its primary
    identity is the agent. Disk-based resolution avoids the
    ``ethos whoami`` subprocess race on ``claude --resume`` (spec
    invariant 9). Companion (human) registration is deferred to the
    heartbeat loop.

    Raises :class:`SystemExit` for the same conditions as
    :func:`_load_base_config`, plus when no identity source succeeds.
    """
    base = _load_base_config(
        data_dir_override=data_dir_override,
        relay_url_override=relay_url_override,
        prefix=prefix,
        start=start,
    )

    if user_override is not None:
        return _assemble_config(base, user_override, "", "")

    agent = resolve_agent_identity_from_disk(base.repo_root)
    if agent is not None:
        return _assemble_config(base, agent.handle, agent.display_name, agent.kind)

    identity = get_github_identity()
    if identity is not None:
        return _assemble_config(base, identity.login, identity.display_name, "")

    os_user = get_os_user()
    if os_user is None:
        raise SystemExit(_NO_USER_MSG)
    return _assemble_config(base, os_user, "", "")


def load_cli_config(
    *,
    user_override: str | None = None,
    data_dir_override: Path | None = None,
    relay_url_override: object = RELAY_URL_UNSET,
    prefix: Path = _DEFAULT_PREFIX,
    start: Path | None = None,
) -> ResolvedConfig:
    """Discover and resolve configuration for the ``biff`` CLI.

    Identity chain:
    ``user_override`` -> :func:`get_github_identity` -> :func:`get_os_user`.

    The CLI runs in the user's shell -- a human at the terminal. CLI
    sessions identify as the human, never the agent. This is the
    pre-spec behavior with the now-deleted ``get_ethos_identity()``
    step removed (spec § 1.1). A GitHub login matching a known
    ``kind: agent`` identity is rejected even when ``get_github_identity``
    resolves one -- see :func:`_resolve_human_identity` (DES-053).

    Raises :class:`SystemExit` for the same conditions as
    :func:`_load_base_config`, plus when no identity source succeeds.
    """
    base = _load_base_config(
        data_dir_override=data_dir_override,
        relay_url_override=relay_url_override,
        prefix=prefix,
        start=start,
    )
    user, display_name, kind = _resolve_human_identity(user_override, base.repo_root)
    return _assemble_config(base, user, display_name, kind)
