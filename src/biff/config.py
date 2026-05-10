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
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

from biff._stdlib import (
    find_git_root,
    get_repo_owner,
    get_repo_slug,
    is_enabled,
    sanitize_repo_name,
    yaml_config_dir,
)
from biff.models import BiffConfig, RelayAuth

# Re-export stdlib functions so existing callers of biff.config still work.
__all__ = [
    "find_git_root",
    "get_repo_slug",
    "is_enabled",
    "sanitize_repo_name",
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


def _read_identity_yaml(path: Path) -> dict[str, object] | None:
    """Load identity YAML, swallowing parse errors with a warning.

    Unlike ``load_yaml_config`` (which raises ``SystemExit`` on parse
    errors), agent identity resolution must never prevent biff from
    starting -- the fallback chain handles missing or malformed
    identity files (spec invariant 8).
    """
    try:
        raw: object = yaml.safe_load(path.read_text())
    except (OSError, UnicodeDecodeError):
        return None
    except yaml.YAMLError as exc:
        logger.warning("Failed to parse identity YAML %s: %s", path, exc)
        return None
    if isinstance(raw, dict):
        return cast("dict[str, object]", raw)
    return None


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
    handle_raw = identity.get("handle", agent)
    handle = handle_raw if isinstance(handle_raw, str) and handle_raw else agent
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


def write_yaml_local_enabled(repo_root: Path, *, enabled: bool) -> Path:
    """Set the ``enabled`` flag in ``config.local.yaml``.

    Reads existing local config first to preserve other keys (e.g.
    relay overrides set via ``biff_relay --local``).  Creates
    ``.punt-labs/biff/`` directory if needed.
    """
    existing = load_yaml_local(repo_root)
    existing["enabled"] = enabled
    return write_yaml_config(repo_root, existing, local=True)


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
) -> tuple[str | None, RelayAuth | None]:
    """Extract relay URL and auth from the ``relay`` section."""
    relay_section: object = raw.get("relay")
    if not isinstance(relay_section, dict):
        return None, None

    section = cast("dict[str, object]", relay_section)
    url: object = section.get("url")
    relay_url = url if isinstance(url, str) else None

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

    return relay_url, relay_auth


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
    tuple[str, ...],
    tuple[str, ...],
]:
    """Extract team, relay_url, relay_auth, peers, and orgs."""
    team = _extract_team(raw)
    relay_url, relay_auth = _extract_relay(raw)
    peers, orgs = _extract_peers(raw)
    return team, relay_url, relay_auth, peers, orgs


RELAY_URL_UNSET = object()


@dataclass(frozen=True)
class _ConfigFields:
    """Intermediate container for fields resolved from config files."""

    team: tuple[str, ...] = ()
    relay_url: str | None = None
    relay_auth: RelayAuth | None = None
    peers: tuple[str, ...] = ()
    orgs: tuple[str, ...] = ()
    poll_interval: float = 2.0


def _has_orgs_key(raw: dict[str, object]) -> bool:
    """Check if peers.orgs is explicitly set in the config dict.

    Distinguishes "key absent" from "key present but empty list" so
    users can configure ``peers.orgs: []`` to disable org discovery.
    """
    peers = raw.get("peers")
    return isinstance(peers, dict) and "orgs" in peers


def _enrich_team(cf: _ConfigFields) -> _ConfigFields:
    """Enrich team from ethos when no explicit team is configured."""
    if cf.team:
        return cf
    ethos_team = get_ethos_team()
    if ethos_team is None:
        return cf
    return _ConfigFields(
        team=ethos_team,
        relay_url=cf.relay_url,
        relay_auth=cf.relay_auth,
        peers=cf.peers,
        orgs=cf.orgs,
        poll_interval=cf.poll_interval,
    )


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
        # Derive orgs from remote only when the peers.orgs key is
        # ABSENT from the merged config. An explicit empty list
        # (peers.orgs: []) is honored — it means "no org discovery."
        if not _has_orgs_key(merged):
            owner = get_repo_owner(repo_root)
            cf = _ConfigFields(
                relay_url=cf.relay_url,
                relay_auth=cf.relay_auth,
                team=cf.team,
                peers=cf.peers,
                orgs=(owner,) if owner else (),
                poll_interval=cf.poll_interval,
            )
        return _enrich_team(cf)

    # Zero-config: derive org from remote, use demo relay.
    # Still read config.local.yaml — user may have set relay via
    # biff_relay --local without a shared config.yaml.
    yaml_local = load_yaml_local(repo_root)
    if yaml_local:
        fields = extract_biff_fields(yaml_local)
        poll_interval = _extract_poll_interval(yaml_local)
        cf = _ConfigFields(*fields, poll_interval=poll_interval)
        # Apply demo relay default only when URL is demo or absent.
        # _apply_demo_relay_default checks relay_url == DEMO_RELAY_URL
        # before applying bundled creds — prevents sending demo creds
        # to a custom relay.
        relay_url, relay_auth = _apply_demo_relay_default(cf.relay_url, cf.relay_auth)
        # Derive owner only when peers.orgs key is absent.
        if _has_orgs_key(yaml_local):
            orgs = cf.orgs
        else:
            owner = get_repo_owner(repo_root)
            orgs = (owner,) if owner else ()
        return _enrich_team(
            _ConfigFields(
                relay_url=relay_url,
                relay_auth=relay_auth,
                orgs=orgs,
                team=cf.team,
                peers=cf.peers,
                poll_interval=cf.poll_interval,
            )
        )

    owner = get_repo_owner(repo_root)
    orgs = (owner,) if owner else ()
    return _enrich_team(
        _ConfigFields(
            relay_url=DEMO_RELAY_URL,
            relay_auth=RelayAuth(user_credentials=str(demo_creds_path())),
            orgs=orgs,
        )
    )


def _apply_demo_relay_default(
    relay_url: str | None, relay_auth: RelayAuth | None
) -> tuple[str, RelayAuth | None]:
    """Ensure demo relay is the fallback when no relay is specified."""
    if relay_url is None:
        relay_url = DEMO_RELAY_URL
    if relay_url == DEMO_RELAY_URL and relay_auth is None:
        relay_auth = RelayAuth(user_credentials=str(demo_creds_path()))
    return relay_url, relay_auth


_NO_USER_MSG = (
    "No user configured. Install the gh CLI and authenticate, or pass --user <handle>"
)


@dataclass(frozen=True)
class _BaseConfig:
    """Non-identity portion of a resolved config, shared by both entry points."""

    repo_root: Path
    repo_name: str
    data_dir: Path
    relay_url: str | None
    relay_auth: RelayAuth | None
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
    :func:`sanitize_repo_name`, ``config.yaml`` is malformed, or the
    relay section contains conflicting auth keys.
    """
    repo_root = find_git_root(start)
    if repo_root is None:
        raise SystemExit("Not in a git repository. Run biff from inside a repo.")

    cf = _resolve_config_fields(repo_root)
    relay_url_resolved, relay_auth = _apply_demo_relay_default(
        cf.relay_url, cf.relay_auth
    )
    relay_url: str | None = relay_url_resolved

    # CLI relay-url override: empty string -> local relay,
    # non-empty -> use it.  Always clear relay_auth on override.
    if relay_url_override is not RELAY_URL_UNSET:
        override = str(relay_url_override) if relay_url_override else ""
        relay_url = override or None
        relay_auth = None

    repo_slug = get_repo_slug(repo_root)
    repo_name = sanitize_repo_name(repo_slug or repo_root.name)
    data_dir = (
        data_dir_override
        if data_dir_override is not None
        else compute_data_dir(repo_root, prefix)
    )

    return _BaseConfig(
        repo_root=repo_root,
        repo_name=repo_name,
        data_dir=data_dir,
        relay_url=relay_url,
        relay_auth=relay_auth,
        team=cf.team,
        peers=cf.peers,
        orgs=cf.orgs,
        poll_interval=cf.poll_interval,
    )


def _resolve_human_identity(
    user_override: str | None,
) -> tuple[str, str, str]:
    """Return (user, display_name, kind) from the human-identity chain.

    Chain: ``user_override`` -> ``get_github_identity()`` -> ``get_os_user()``.
    Raises :class:`SystemExit` when no identity source succeeds.
    """
    if user_override is not None:
        return user_override, "", ""
    identity = get_github_identity()
    if identity is not None:
        return identity.login, identity.display_name, ""
    os_user = get_os_user()
    if os_user is None:
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
        team=base.team,
        peers=base.peers,
        orgs=base.orgs,
        poll_interval=base.poll_interval,
    )
    return ResolvedConfig(
        config=config,
        data_dir=base.data_dir,
        repo_root=base.repo_root,
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
    step removed (spec § 1.1).

    Raises :class:`SystemExit` for the same conditions as
    :func:`_load_base_config`, plus when no identity source succeeds.
    """
    base = _load_base_config(
        data_dir_override=data_dir_override,
        relay_url_override=relay_url_override,
        prefix=prefix,
        start=start,
    )
    user, display_name, kind = _resolve_human_identity(user_override)
    return _assemble_config(base, user, display_name, kind)
