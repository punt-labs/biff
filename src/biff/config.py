"""Configuration discovery and loading.

Reads YAML config from ``.punt-labs/biff/`` (shared + local override),
falls back to legacy ``.biff`` TOML, or runs in zero-config mode with
defaults derived from the git remote.

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
import logging
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

from biff._stdlib import (
    find_git_root,
    get_repo_owner,
    get_repo_slug,
    is_enabled,
    load_biff_local,
    sanitize_repo_name,
    yaml_config_dir,
)
from biff.models import BiffConfig, RelayAuth

logger = logging.getLogger(__name__)

# Re-export stdlib functions so existing callers of biff.config still work.
__all__ = [
    "find_git_root",
    "get_repo_slug",
    "is_enabled",
    "load_biff_local",
    "sanitize_repo_name",
]

_DEFAULT_PREFIX = Path("/tmp")  # noqa: S108
DEMO_RELAY_URL = "tls://connect.ngs.global"


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

    Catches ``OSError`` (permissions, TOCTOU race) and returns ``{}``.
    Lets ``yaml.YAMLError`` propagate so callers can decide severity.
    """
    try:
        raw: object = yaml.safe_load(path.read_text())
    except OSError:
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
        if entry in content:
            return
        if not content.endswith("\n"):
            content += "\n"
        content += entry + "\n"
        gitignore.write_text(content)
    else:
        gitignore.write_text(entry + "\n")


# ── Legacy TOML support (migration path only) ──────────────────────


def load_biff_file(repo_root: Path) -> dict[str, object]:
    """Parse the ``.biff`` TOML file at *repo_root*, or return ``{}``."""
    path = repo_root / ".biff"
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(
            f"Failed to parse {path}:\n{exc}\n"
            "Fix or remove this file before starting biff."
        ) from exc


def ensure_biff_file(
    repo_root: Path, *, team: tuple[str, ...], relay_url: str | None
) -> None:
    """Create ``.biff`` if it doesn't exist, using provided defaults."""
    if (repo_root / ".biff").exists():
        return
    from biff.relay import atomic_write  # noqa: PLC0415

    url = relay_url or DEMO_RELAY_URL
    atomic_write(repo_root / ".biff", build_biff_toml(list(team), url))


def ensure_gitignore(repo_root: Path) -> None:
    """Add ``.biff.local`` to the repo's ``.gitignore`` if not already present."""
    gitignore = repo_root / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text()
        if ".biff.local" in content:
            return
        if not content.endswith("\n"):
            content += "\n"
        content += ".biff.local\n"
        gitignore.write_text(content)
    else:
        gitignore.write_text(".biff.local\n")


def write_biff_local(repo_root: Path, *, enabled: bool) -> None:
    """Write ``.biff.local`` with the ``enabled`` flag.

    Uses :func:`~biff.relay.atomic_write` for safe replacement.
    """
    from biff.relay import atomic_write  # noqa: PLC0415

    content = f"enabled = {'true' if enabled else 'false'}\n"
    atomic_write(repo_root / ".biff.local", content)


def _toml_basic_string(value: str) -> str:
    """Escape *value* for use as a TOML basic string."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_biff_toml(
    members: list[str],
    relay_url: str,
    peers: list[str] | None = None,
) -> str:
    """Build ``.biff`` TOML content from user inputs."""
    lines: list[str] = []
    if members:
        quoted = ", ".join(_toml_basic_string(m) for m in members)
        lines.append("[team]")
        lines.append(f"members = [{quoted}]")
    if relay_url:
        if lines:
            lines.append("")
        lines.append("[relay]")
        lines.append(f"url = {_toml_basic_string(relay_url)}")
    if peers:
        if lines:
            lines.append("")
        quoted_peers = ", ".join(_toml_basic_string(p) for p in peers)
        lines.append("[peers]")
        lines.append(f"repos = [{quoted_peers}]")
    return "\n".join(lines) + "\n" if lines else ""


# ── Field extraction (shared by TOML and YAML paths) ───────────────


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
            f"Conflicting auth in config [relay]: {names}\n"
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


_GITHUB_ACTIONS_USER = "github-actions"


def ensure_github_actions_member(repo_root: Path) -> bool:
    """Add ``github-actions`` to ``.biff`` team members if not present.

    Does a targeted text edit to preserve existing TOML formatting.
    Returns ``True`` if the file was modified.
    """
    biff_file = repo_root / ".biff"
    if not biff_file.exists():
        return False

    content = biff_file.read_text()
    raw = load_biff_file(repo_root)
    team_section: object = raw.get("team")
    if not isinstance(team_section, dict):
        return False

    section = cast("dict[str, object]", team_section)
    members: object = section.get("members", [])
    if not isinstance(members, list):
        return False

    items = cast("list[object]", members)
    member_list: list[str] = [m for m in items if isinstance(m, str)]
    if _GITHUB_ACTIONS_USER in member_list:
        return False

    # Append to existing members array via text replacement.
    # Find the closing bracket of the members array and insert before it.
    import re  # noqa: PLC0415

    pattern = re.compile(r"(members\s*=\s*\[.*?)(])", re.DOTALL)
    match = pattern.search(content)
    if match is None:
        return False

    prefix = match.group(1).rstrip().rstrip(",").rstrip()
    quoted = _toml_basic_string(_GITHUB_ACTIONS_USER)
    # Handle empty array: [] -> ["github-actions"] (no leading comma)
    separator = "" if prefix.endswith("[") else ", "
    new_content = (
        content[: match.start()]
        + prefix
        + f"{separator}{quoted}]"
        + content[match.end() :]
    )
    biff_file.write_text(new_content)
    return True


RELAY_URL_UNSET = object()


@dataclass(frozen=True)
class _ConfigFields:
    """Intermediate container for fields resolved from config files."""

    team: tuple[str, ...] = ()
    relay_url: str | None = None
    relay_auth: RelayAuth | None = None
    peers: tuple[str, ...] = ()
    orgs: tuple[str, ...] = ()


def _resolve_config_fields(repo_root: Path) -> _ConfigFields:
    """Resolve config fields from YAML, legacy TOML, or zero-config.

    Detection order:

    1. ``.punt-labs/biff/config.yaml`` -- explicit mode.
    2. ``.biff`` -- legacy TOML, triggers migration notice.
    3. Neither -- zero-config with derived defaults.
    """
    yaml_shared = load_yaml_config(repo_root)
    if yaml_shared:
        yaml_local = load_yaml_local(repo_root)
        merged = merge_config(yaml_shared, yaml_local)
        fields = extract_biff_fields(merged)
        return _ConfigFields(*fields)

    if (repo_root / ".biff").exists():
        logger.info(
            "Legacy .biff detected at %s. "
            "Run /biff y to migrate to .punt-labs/biff/config.yaml.",
            repo_root / ".biff",
        )
        raw = load_biff_file(repo_root)
        # Merge config.local.yaml on top — user may have set overrides
        # via biff_relay --local even with a legacy .biff file.
        yaml_local = load_yaml_local(repo_root)
        if yaml_local:
            raw = merge_config(raw, yaml_local)
        fields = extract_biff_fields(raw)
        return _ConfigFields(*fields)

    # Zero-config: derive org from remote, use demo relay.
    # Still read config.local.yaml — user may have set relay via
    # biff_relay --local without a shared config.yaml.
    yaml_local = load_yaml_local(repo_root)
    if yaml_local:
        fields = extract_biff_fields(yaml_local)
        cf = _ConfigFields(*fields)
        # Apply demo relay default only when URL is demo or absent.
        # _apply_demo_relay_default checks relay_url == DEMO_RELAY_URL
        # before applying bundled creds — prevents sending demo creds
        # to a custom relay.
        relay_url, relay_auth = _apply_demo_relay_default(cf.relay_url, cf.relay_auth)
        owner = get_repo_owner(repo_root)
        return _ConfigFields(
            relay_url=relay_url,
            relay_auth=relay_auth,
            orgs=cf.orgs or ((owner,) if owner else ()),
            team=cf.team,
            peers=cf.peers,
        )

    owner = get_repo_owner(repo_root)
    orgs = (owner,) if owner else ()
    return _ConfigFields(
        relay_url=DEMO_RELAY_URL,
        relay_auth=RelayAuth(user_credentials=str(demo_creds_path())),
        orgs=orgs,
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


def load_config(
    *,
    user_override: str | None = None,
    data_dir_override: Path | None = None,
    relay_url_override: object = RELAY_URL_UNSET,
    prefix: Path = _DEFAULT_PREFIX,
    start: Path | None = None,
) -> ResolvedConfig:
    """Discover and resolve all configuration.

    Raises :class:`SystemExit` if no user identity can be resolved
    or if ``start`` is not inside a git repository.
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

    # Resolve user: CLI override > GitHub identity > OS username
    display_name = ""
    if user_override is not None:
        user: str | None = user_override
    else:
        identity = get_github_identity()
        if identity is not None:
            user = identity.login
            display_name = identity.display_name
        else:
            user = get_os_user()
    if user is None:
        msg = (
            "No user configured. Install the gh CLI and authenticate,"
            " or pass --user <handle>"
        )
        raise SystemExit(msg)

    repo_slug = get_repo_slug(repo_root)
    repo_name = sanitize_repo_name(repo_slug or repo_root.name)
    data_dir = (
        data_dir_override
        if data_dir_override is not None
        else compute_data_dir(repo_root, prefix)
    )

    config = BiffConfig(
        user=user,
        display_name=display_name,
        repo_name=repo_name,
        relay_url=relay_url,
        relay_auth=relay_auth,
        team=cf.team,
        peers=cf.peers,
        orgs=cf.orgs,
    )
    return ResolvedConfig(config=config, data_dir=data_dir, repo_root=repo_root)
