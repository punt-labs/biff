"""Identity resolution: who is this biff session.

Implements the DES-076 identity chains.  Sources (GitHub via ``gh``, OS
user, ethos identity files on disk) feed two chains: the human chain used
by the CLI and the agent-first chain used by the MCP server.  Nothing here
reads biff's YAML config, so this module never imports ``biff.config``.
"""

from __future__ import annotations

import getpass
import logging
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import cast

import yaml

logger = logging.getLogger(__name__)

# Agent handle grammar — matches identity YAML filenames. Length 1-64, lowercase
# letters, digits, underscore, hyphen; first character cannot be a hyphen or
# underscore. Repo-controlled input, so this is a hard precondition (spec § 3
# step 3, invariant 10) guarding against path-traversal payloads in the
# ``agent`` field of ``.punt-labs/ethos.yaml``.
_AGENT_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_QUOTED_RE = re.compile(r"([\"'])(.*)\1")  # a git value stored quoted


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
            logger.debug("gh api user failed (exit %d)", result.returncode)
            return None
        login, _, display_name = result.stdout.strip().partition("\t")
        if not login.strip():
            return None
        return GitHubIdentity(login=login.strip(), display_name=display_name.strip())
    except FileNotFoundError:
        return None


@dataclass(frozen=True)
class EthosIdentity:
    """Persona identity resolved from Ethos records or roster data."""

    handle: str
    display_name: str
    kind: str  # "human", "agent", or ""


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
    ``None`` would authorize identities from an unverifiable registry.

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
    except yaml.YAMLError:
        logger.warning("Failed to parse identity YAML %r", path)
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
    against the same way (see :func:`resolve_human_identity`).
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
            # resolve_human_identity).
            logins.add(github.strip().casefold())
        else:
            incomplete += 1
            logger.warning(
                "Agent identity %r has a malformed 'github' field; "
                "it cannot be cross-checked against a resolved GitHub login",
                path,
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
    :func:`resolve_human_identity` detect and reject that case.

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
            "Rejecting malformed agent handle in %r: must match %s",
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
    if (yaml_handle := identity.get("handle")) not in (None, "", agent):
        logger.warning(
            "Identity %s declares handle %r; using validated agent handle %r",
            identity_path,
            yaml_handle,
            agent,
        )
    name_raw = identity.get("name", agent)
    display_name = name_raw if isinstance(name_raw, str) and name_raw else agent
    if identity.get("kind") != "agent":
        msg = "Rejecting identity %s: kind=%r (expected 'agent')"
        logger.warning(msg, identity_path, identity.get("kind", ""))
        return None
    # Always the validated filename-derived handle, never the YAML's own.
    return EthosIdentity(handle=agent, display_name=display_name, kind="agent")


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


def get_os_user() -> str | None:
    """Return the OS username, or ``None`` if unavailable."""
    try:
        return getpass.getuser()
    except OSError:
        return None


_NO_USER_MSG = (
    "No user configured. Install the gh CLI and authenticate, or pass --user <handle>"
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


def resolve_human_identity(
    user_override: str | None, repo_root: Path
) -> tuple[str, str, str]:
    """Return (user, display_name, kind) from the human-identity chain.

    Chain: ``user_override`` or ``BIFF_USER`` -> ``get_github_identity()`` ->
    ``get_os_user()``.
    An override resolves as in :func:`resolve_mcp_identity`.
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
    if (user_override or os.environ.get("BIFF_USER", "")).strip():
        return resolve_mcp_identity(repo_root, user_override)
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
                "GitHub login matches a known bot/agent identity; "
                "falling back to OS user for CLI identity"
            )
        elif not scan.complete:
            unverified_login = identity.login
            logger.warning(
                "Cannot confirm GitHub login is not a bot/agent identity "
                "-- the identity scan was incomplete; falling back to OS "
                "user for CLI identity out of caution"
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


def resolve_mcp_identity(
    base_repo_root: Path, user_override: str | None
) -> tuple[str, str, str]:
    """Return (user, display_name, kind) from the agent-first identity chain.

    Chain: ``user_override`` or ``BIFF_USER`` ->
    :func:`resolve_agent_identity_from_disk` ->
    :func:`get_github_identity` -> :func:`get_os_user`.
    An override naming a ``kind: agent`` identity in
    ``.punt-labs/ethos/identities/`` is that agent, with its kind and name.

    The MCP server runs inside the agent's process; its primary identity is
    the agent. Disk-based resolution avoids the ``ethos whoami`` subprocess
    race on ``claude --resume`` (spec invariant 9).

    Raises :class:`SystemExit` when no identity source succeeds.
    """
    if user_override := (user_override or os.environ.get("BIFF_USER", "")).strip():
        path = _AGENT_HANDLE_RE.fullmatch(user_override) and _resolve_identity_path(
            base_repo_root, user_override
        )
        if agent := path and _build_agent_identity(path, user_override):
            return agent.handle, agent.display_name, agent.kind
        return user_override, "", ""

    agent = resolve_agent_identity_from_disk(base_repo_root)
    if agent is not None:
        return agent.handle, agent.display_name, agent.kind

    identity = get_github_identity()
    if identity is not None:
        return identity.login, identity.display_name, ""

    os_user = get_os_user()
    if os_user is None:
        raise SystemExit(_NO_USER_MSG)
    return os_user, "", ""


def owner_name(repo_root: Path | None) -> str:
    """Return ``BIFF_OWNER``, else ``git config user.name`` for *repo_root*, or ``""``.

    ``BIFF_OWNER`` names the human behind a hosted agent whose git identity is
    the bot's own.

    The configured value, not the effective author: provisioned hosts export
    ``GIT_AUTHOR_NAME`` as the bot, and the config still names the human.
    Never raises; any failure reads as unset.
    """
    if owner := os.environ.get("BIFF_OWNER", "").strip():
        return owner
    cmd = ["git", "config", "user.name"]
    if repo_root is not None:
        cmd[1:1] = ["-C", str(repo_root)]
    try:
        result = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, check=False, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("git user.name could not be read")
        return ""
    if result.returncode != 0:
        logger.debug("git user.name is unset (git exited %d)", result.returncode)
        return ""
    name = result.stdout.strip()
    return quoted[2].strip() if (quoted := _QUOTED_RE.fullmatch(name)) else name
