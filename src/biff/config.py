"""Configuration discovery and loading.

Reads YAML config from ``.punt-labs/biff/`` (shared + local override)
or runs in zero-config mode with defaults derived from the git remote.

With no relay configured the local filesystem relay is used.  Its data
directory layout (``{relay-root}`` is ``~/.punt-labs/biff/relay`` unless
overridden)::

    {relay-root}/{repo-key}/
        userinbox-kai.jsonl        # per-user mailbox (broadcast)
        inbox-kai-a1b2c3d4.jsonl   # per-TTY mailbox (targeted)
        userinbox-eric.jsonl
        inbox-eric-12345678.jsonl
        sessions.json
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import cast

import yaml

from biff import identity
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
    # ``repo_root`` when ``get_repo_common_root`` returns ``""``).
    repo_common_root: Path
    repo_root: Path | None = None


# ── YAML config pipeline ───────────────────────────────────────────


def load_yaml(path: Path) -> dict[str, object]:
    """Load a YAML file and return a dict, or ``{}`` on error.

    Catches ``OSError`` (permissions, TOCTOU race) and
    ``UnicodeDecodeError`` (invalid text encoding), returning ``{}``.

    Raises:
        yaml.YAMLError: The file contains malformed YAML.
    """
    try:
        raw: object = yaml.safe_load(path.read_text())
    except (OSError, UnicodeDecodeError):
        return {}
    if isinstance(raw, dict):
        return cast("dict[str, object]", raw)
    return {}


def load_yaml_config(repo_root: Path) -> dict[str, object]:
    """Read shared YAML config, returning an empty mapping when absent.

    Raises:
        SystemExit: Its YAML is malformed.
    """
    path = yaml_config_dir(repo_root) / "config.yaml"
    if not path.exists():
        return {}
    try:
        return load_yaml(path)
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
        return load_yaml(path)
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
    # Whether the SHARED config.yaml itself (never config.local.yaml, never
    # the merged result) declares a relay.url. relay_url above is the
    # merged, local-inclusive value -- gating _apply_env_relay_overrides on
    # it would block every developer with a personal config.local.yaml
    # relay entry, not just repos with a team-committed relay. Both this
    # flag and relay_allow_env_override must come from the shared file
    # alone, or a gitignored config.local.yaml could set
    # allow_env_override: true and authorize ambient env vars without a
    # team commit.
    relay_committed: bool = False


def _has_orgs_key(raw: dict[str, object]) -> bool:
    """Check if peers.orgs is explicitly set in the config dict.

    Distinguishes "key absent" from "key present but empty list" so
    users can configure ``peers.orgs: []`` to disable org discovery.
    """
    peers = raw.get("peers")
    return isinstance(peers, dict) and "orgs" in peers


def _relay_config() -> ModuleType:
    """Return the ``biff.relay_config`` module, imported on first use."""
    from biff import relay_config  # noqa: PLC0415

    return relay_config


def relay_section_to_write(
    url: str, auth: str, relay_type: str
) -> dict[str, object] | str:
    """Return the ``relay`` mapping the ``biff_relay`` tool writes, or an error.

    The type is always explicit; a URL alone means ``nats``.
    """
    types = _relay_config().RELAY_TYPES
    kind = relay_type or ("nats" if url else "")
    if kind not in types:
        if not kind:
            return "Error: give a url, or relay_type local, demo or nats."
        return (
            f"Error: unknown relay_type {kind!r}, expected one of {', '.join(types)}."
        )
    if kind != "nats":
        if url or auth:
            return f"Error: relay_type {kind} takes no url or auth."
        return {"type": kind}
    if not url.startswith(("tls://", "nats://", "ws://", "wss://")):
        return (
            "Error: invalid relay URL scheme, "
            "expected tls://, nats://, ws:// or wss://."
        )
    section: dict[str, object] = {"type": "nats", "url": url}
    if auth:
        section["auth"] = {"credentials": auth}
    return section


def _owner_orgs(repo_root: Path) -> tuple[str, ...]:
    """Return the remote's owner as the default org scope (none without one)."""
    owner = get_repo_owner(repo_root)
    return (owner,) if owner else ()


def _resolve_config_fields(repo_root: Path) -> _ConfigFields:
    """Resolve config fields from ``config.yaml`` and ``config.local.yaml``.

    Either file may be absent: a user may have chosen a relay for themselves
    alone, and a repo with neither file runs on derived defaults.
    """
    relay_config = _relay_config()
    yaml_shared = load_yaml_config(repo_root)
    yaml_local = load_yaml_local(repo_root)
    merged = merge_config(yaml_shared, yaml_local)
    relay = relay_config.resolve_relay(
        [("config.yaml", yaml_shared), ("config.local.yaml", yaml_local)]
    )
    peers, orgs = _extract_peers(merged)
    # Derive orgs from the remote only when the peers.orgs key is ABSENT from
    # the merged config. An explicit empty list (peers.orgs: []) is honored:
    # it means "no org discovery."
    if not _has_orgs_key(merged):
        orgs = _owner_orgs(repo_root)
    shared_relay = relay_config.relay_section(yaml_shared)
    # Both gates read yaml_shared alone, never the merged config -- see
    # _ConfigFields.relay_committed for why.  The opt-in gate defaults closed:
    # absent or any non-True value means no.
    return _ConfigFields(
        team=_extract_team(merged),
        relay_url=relay.url,
        relay_auth=relay.auth,
        relay_tls_handshake_first=relay.tls_handshake_first,
        peers=peers,
        orgs=orgs,
        poll_interval=_extract_poll_interval(merged),
        relay_allow_env_override=shared_relay.get("allow_env_override") is True,
        relay_committed=relay_config.relay_declared(yaml_shared),
    )


_TLS_TRUE_VALUES = frozenset({"1", "true", "yes"})
_TLS_FALSE_VALUES = frozenset({"0", "false", "no"})

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


def _resolved_auth_override(auth_var: str, path_value: str | None) -> RelayAuth:
    """Build the ``RelayAuth`` for a single fired auth env var, or raise.

    Fails fast for the two path-valued vars (``BIFF_RELAY_NKEYS_SEED``,
    ``BIFF_RELAY_USER_CREDENTIALS``) rather than deferring to an opaque NATS
    connect error later (docs/relay-env-overrides.md, open question 2).
    """
    if auth_var in _AUTH_ENV_PATH_FIELDS and not Path(cast("str", path_value)).exists():
        raise SystemExit(
            f"{auth_var} points to a file that does not exist: {path_value}"
        )
    # Wholesale replace -- never merge with the file-resolved RelayAuth
    # (Sec 2): a token from a file and an nkeys_seed from the environment
    # must never combine into one RelayAuth instance, which would violate
    # RelayAuth's own single-field invariant.
    return RelayAuth(**{_AUTH_ENV_FIELDS[auth_var]: path_value})


def _tls_env_override() -> bool | None:
    """Read ``BIFF_RELAY_TLS_HANDSHAKE_FIRST``, or ``None`` if it didn't fire.

    ``True``/``False`` are both explicit, distinguishable overrides --
    unlike the boolean's default, absence (``None``) means "leave whatever
    value the URL/file resolution already settled on."
    """
    normalized = os.environ.get("BIFF_RELAY_TLS_HANDSHAKE_FIRST", "").strip().casefold()
    if normalized in _TLS_TRUE_VALUES:
        return True
    if normalized in _TLS_FALSE_VALUES:
        return False
    return None


def _apply_env_relay_overrides(cf: _ConfigFields) -> _ConfigFields:
    """Layer ``BIFF_RELAY_*`` env vars over file-resolved relay fields.

    Precedence: ``config.yaml`` < ``config.local.yaml`` < env vars < the
    CLI ``--relay-url`` override applied later in :func:`_load_base_config`
    (docs/relay-env-overrides.md Sec 1). No-ops unless the repo-scoping
    gate (Sec 0) is satisfied: the SHARED ``config.yaml`` has no committed
    ``relay.url`` of its own (:attr:`_ConfigFields.relay_committed` is
    ``False`` -- this covers both true zero-config and a personal
    ``config.local.yaml`` relay entry), or that same shared file explicitly
    opts in via ``relay.allow_env_override: true``.

    Raises :class:`SystemExit` when two or more of ``BIFF_RELAY_TOKEN``,
    ``BIFF_RELAY_NKEYS_SEED``, and ``BIFF_RELAY_USER_CREDENTIALS`` are set
    simultaneously (naming the conflicting variables, never their values),
    when ``BIFF_RELAY_NKEYS_SEED``/``BIFF_RELAY_USER_CREDENTIALS`` names a
    path that does not exist, or when an auth env var fires with no relay
    URL resolved (from ``BIFF_RELAY_URL`` or the file layers) -- an auth
    override with nothing to authenticate against would otherwise be ignored by
    the local relay, hiding the misconfiguration.
    """
    if cf.relay_committed and not cf.relay_allow_env_override:
        return cf

    # Conflict check reads only names, never values, before deciding
    # whether to fetch the one fired var's value -- a SystemExit here must
    # never leave a dict of raw credential values sitting in this frame's
    # locals for a traceback to retain.
    fired_auth = [name for name in _AUTH_ENV_FIELDS if _env_or_none(name) is not None]
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
        relay_auth = _resolved_auth_override(auth_var, _env_or_none(auth_var))
        fired.append(auth_var)

    url_env = _env_or_none("BIFF_RELAY_URL")
    if url_env is not None:
        relay_url = url_env
        fired.append("BIFF_RELAY_URL")
        if not fired_auth:
            # URL-changes-clears-auth (Sec 2, direct #383 lineage): auth is a
            # property of the relay being replaced, not portable to whatever
            # BIFF_RELAY_URL now points at.
            relay_auth = None
        # TLS mode is likewise a property of the relay being replaced --
        # reset it whenever the URL changes, even when an auth env var also
        # fired in the same call. Without this, a stale file-resolved
        # tls_handshake_first=True would leak onto the new relay in exactly
        # the case docs/relay-env-overrides.md Sec 7 documents (URL + token
        # set together), reopening the #383 footgun.
        tls_handshake_first = False

    if fired_auth and relay_url is None:
        # An auth override with nothing to authenticate against would
        # otherwise fall through to the local relay, which ignores it: a
        # missing or misspelled BIFF_RELAY_URL would pass unnoticed.
        (auth_var,) = fired_auth
        raise SystemExit(
            f"{auth_var} is set but no relay URL is configured -- set "
            "BIFF_RELAY_URL as well. An auth override with no relay URL "
            "would otherwise be ignored by the local relay."
        )

    tls_override = _tls_env_override()
    if tls_override is not None:
        tls_handshake_first = tls_override
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


def _resolve_relay_fields(
    cf: _ConfigFields,
) -> tuple[str | None, RelayAuth | None, bool]:
    """Apply env overrides to resolved fields; a ``None`` URL is the local relay."""
    relay_config = _relay_config()
    cf = _apply_env_relay_overrides(cf)
    relay_auth = cf.relay_auth
    if cf.relay_url == relay_config.DEMO_RELAY_URL and relay_auth is None:
        relay_auth = RelayAuth(user_credentials=str(relay_config.DEMO_CREDS_PATH))
    return cf.relay_url, relay_auth, cf.relay_tls_handshake_first


def resolve_relay_config(
    repo_root: Path | None,
) -> tuple[str | None, RelayAuth | None, bool]:
    """Resolve relay URL, auth, and TLS handshake mode for the repository.

    A ``None`` URL means the local filesystem relay.

    Share the file and environment resolution chain with the CLI and doctor
    so diagnostics report the relay commands actually connect to.

    Args:
        repo_root: Use zero-config defaults when None.

    Raises:
        SystemExit: Shared YAML is malformed or relay environment overrides
            are incomplete or invalid.
    """
    cf = _resolve_config_fields(repo_root) if repo_root is not None else _ConfigFields()
    return _resolve_relay_fields(cf)


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


def _override_url(override: object) -> str | None:
    """Return the ``--relay-url`` value as a URL; empty means the local relay."""
    return str(override) if override else None


def _load_base_config(
    *,
    data_dir_override: Path | None,
    relay_url_override: object,
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
    relay_url_resolved, relay_auth, relay_tls_handshake_first = _resolve_relay_fields(
        cf
    )
    relay_url: str | None = relay_url_resolved

    # CLI relay-url override: empty string -> local relay,
    # non-empty -> use it.  Always clear relay_auth and
    # relay_tls_handshake_first on override -- both are properties of the
    # relay being replaced, not the override target.  Leaving
    # tls_handshake_first set would silently force it onto whatever the
    # override points at (e.g. the demo relay, whose native-TLS
    # nats-server must never get it -- WRONG_VERSION_NUMBER).
    if relay_url_override is not RELAY_URL_UNSET:
        relay_url = _override_url(relay_url_override)
        relay_auth = None
        relay_tls_handshake_first = False

    repo_slug = get_repo_slug(repo_root)
    repo_name = sanitize_repo_name(repo_slug or repo_root.name)
    data_dir = (
        data_dir_override
        if data_dir_override is not None
        else _relay_config().local_relay_location(repo_root).path
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


def _assemble_config(
    base: _BaseConfig, user: str, display_name: str, kind: str
) -> ResolvedConfig:
    """Build a ``ResolvedConfig`` from a base config and resolved identity."""
    config = BiffConfig(
        user=user,
        display_name=display_name,
        kind=kind,
        launched_by=identity.get_os_user() or "",
        owner_git=identity.git_user_name(base.repo_root),
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
    start: Path | None = None,
) -> ResolvedConfig:
    """Discover and resolve configuration for the MCP server.

    Identity chain:
    ``user_override`` -> :func:`resolve_agent_identity_from_disk` ->
    :func:`get_github_identity` -> :func:`get_os_user`.

    The MCP server runs inside the agent's process; its primary
    identity is the agent. Disk-based resolution avoids the
    ``ethos whoami`` subprocess race on ``claude --resume`` (spec
    invariant 9).

    Raises:
        SystemExit: No repository or identity can be resolved, shared config
            is malformed, or relay environment overrides are invalid.
    """
    base = _load_base_config(
        data_dir_override=data_dir_override,
        relay_url_override=relay_url_override,
        start=start,
    )

    user, display_name, kind = identity.resolve_mcp_identity(
        base.repo_root, user_override
    )
    return _assemble_config(base, user, display_name, kind)


def load_cli_config(
    *,
    user_override: str | None = None,
    data_dir_override: Path | None = None,
    relay_url_override: object = RELAY_URL_UNSET,
    start: Path | None = None,
) -> ResolvedConfig:
    """Discover and resolve configuration for the ``biff`` CLI.

    Identity chain:
    ``user_override`` -> :func:`get_github_identity` -> :func:`get_os_user`.

    The CLI runs in the user's shell -- a human at the terminal. CLI
    sessions identify as the human, never the agent. A GitHub login matching a known
    ``kind: agent`` identity is rejected even when ``get_github_identity``
    resolves one -- see :func:`biff.identity.resolve_human_identity` (DES-053).

    Raises:
        SystemExit: No repository or human identity can be resolved, shared
            config is malformed, or relay environment overrides are invalid.
    """
    base = _load_base_config(
        data_dir_override=data_dir_override,
        relay_url_override=relay_url_override,
        start=start,
    )
    user, display_name, kind = identity.resolve_human_identity(
        user_override, base.repo_root
    )
    return _assemble_config(base, user, display_name, kind)
