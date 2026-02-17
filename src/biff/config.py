"""Configuration discovery and loading.

Finds the ``.biff`` TOML file at the git repo root, resolves user
identity from GitHub (``gh``) or the OS, and computes the shared data
directory.

Config file format (``.biff``)::

    [team]
    members = ["kai", "eric", "priya"]

    [relay]
    url = "nats://localhost:4222"

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
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from biff.models import BiffConfig, RelayAuth

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


def find_git_root(start: Path | None = None) -> Path | None:
    """Walk up from *start* (default: cwd) to find the git repo root."""
    path = (start or Path.cwd()).resolve()
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            return parent
    return None


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


_SLUG_SSH_RE = re.compile(r"^[^@]+@[^:]+:(.+?)(?:\.git)?$")
_SLUG_HTTPS_RE = re.compile(r"^https?://[^/]+/(.+?)(?:\.git)?$")


def _parse_repo_slug(url: str) -> str | None:
    """Extract ``owner/repo`` from an SSH or HTTPS git remote URL.

    Returns ``None`` for URLs that don't match or have nested paths
    (e.g. ``gitlab.com/group/sub/repo``).
    """
    for pattern in (_SLUG_SSH_RE, _SLUG_HTTPS_RE):
        m = pattern.match(url)
        if m:
            slug = m.group(1)
            if slug.count("/") == 1:
                return slug
    return None


def get_repo_slug(repo_root: Path) -> str | None:
    """Resolve ``owner/repo`` from ``git remote get-url origin``.

    Returns ``None`` when git is unavailable, no remote exists, or
    the URL doesn't parse to a two-part slug.
    """
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        return _parse_repo_slug(result.stdout.strip())
    except FileNotFoundError:
        return None


def sanitize_repo_name(name: str) -> str:
    """Sanitize a repo name or slug for use in NATS resource names.

    NATS bucket names allow ASCII alphanumeric, dash, and underscore
    only.  Subject dots are level separators; wildcards (``*``, ``>``)
    are reserved.  Slashes become underscores (owner/repo boundary);
    dots become dashes; spaces become dashes; non-ASCII and remaining
    special characters are stripped.

    Raises ``SystemExit`` if the result is empty — a repo name that
    sanitizes to nothing would silently share a NATS namespace with
    other unusable names, causing the exact collision this function
    exists to prevent.
    """
    clean = name.replace("/", "_").replace(".", "-").replace(" ", "-")
    sanitized = "".join(c for c in clean if (c.isascii() and c.isalnum()) or c in "-_")
    if not sanitized:
        raise SystemExit(
            f"Repo name {name!r} contains no usable characters after sanitization.\n"
            "Rename the directory to include ASCII letters or digits."
        )
    return sanitized


def compute_data_dir(repo_root: Path, prefix: Path) -> Path:
    """Compute data directory: ``{prefix}/biff/{repo_root.name}/``."""
    return prefix / "biff" / repo_root.name


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


def extract_biff_fields(
    raw: dict[str, object],
) -> tuple[tuple[str, ...], str | None, RelayAuth | None]:
    """Extract team, relay_url, and relay_auth from parsed TOML data."""
    team: tuple[str, ...] = ()
    relay_url: str | None = None
    relay_auth: RelayAuth | None = None

    team_section: object = raw.get("team")
    if isinstance(team_section, dict):
        section = cast("dict[str, object]", team_section)
        members: object = section.get("members", [])
        if isinstance(members, list):
            items = cast("list[object]", members)
            team = tuple(m for m in items if isinstance(m, str))

    relay_section: object = raw.get("relay")
    if isinstance(relay_section, dict):
        section = cast("dict[str, object]", relay_section)
        url: object = section.get("url")
        if isinstance(url, str):
            relay_url = url

        # Auth — at most one of token, nkeys_seed, user_credentials
        token = section.get("token")
        nkeys_seed = section.get("nkeys_seed")
        creds = section.get("user_credentials")

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
                f"Conflicting auth in .biff [relay]: {names}\n"
                "Set at most one of 'token', 'nkeys_seed', or 'user_credentials'."
            )
        if auth_values:
            relay_auth = RelayAuth(**auth_values)

    # Default to bundled demo credentials for the demo relay
    if relay_url == DEMO_RELAY_URL and relay_auth is None:
        relay_auth = RelayAuth(user_credentials=str(demo_creds_path()))

    return team, relay_url, relay_auth


RELAY_URL_UNSET = object()


def load_config(
    *,
    user_override: str | None = None,
    data_dir_override: Path | None = None,
    relay_url_override: object = RELAY_URL_UNSET,
    prefix: Path = _DEFAULT_PREFIX,
    start: Path | None = None,
) -> ResolvedConfig:
    """Discover and resolve all configuration.

    Resolution order:

    1. CLI overrides (``user_override``, ``data_dir_override``,
       ``relay_url_override``) take precedence.
    2. ``.biff`` TOML for team roster and relay URL.
    3. GitHub username (via ``gh api user``), falling back to OS username.
    4. Data dir computed from ``{prefix}/biff/{directory-name}/``.

    Raises :class:`SystemExit` if no user identity can be resolved,
    if ``start`` is not inside a git repository, or if the repo
    directory name fails :func:`sanitize_repo_name`.
    """
    repo_root = find_git_root(start)

    # Parse .biff file
    team: tuple[str, ...] = ()
    relay_url: str | None = None
    relay_auth: RelayAuth | None = None
    if repo_root is not None:
        raw = load_biff_file(repo_root)
        team, relay_url, relay_auth = extract_biff_fields(raw)

    # CLI relay-url override: empty string → local relay, non-empty → use it.
    # Always clear relay_auth on override — the .biff credentials are for the
    # .biff relay URL, not whatever the user is overriding to.
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

    # Resolve data dir and repo name
    if repo_root is None:
        raise SystemExit("Not in a git repository. Run biff from inside a repo.")
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
        team=team,
    )
    return ResolvedConfig(config=config, data_dir=data_dir, repo_root=repo_root)
