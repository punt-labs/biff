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
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from biff._stdlib import (
    find_git_root,
    get_repo_slug,
    is_enabled,
    load_biff_local,
    sanitize_repo_name,
)
from biff.models import BiffConfig, RelayAuth

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


def extract_biff_fields(
    raw: dict[str, object],
) -> tuple[tuple[str, ...], str | None, RelayAuth | None, tuple[str, ...]]:
    """Extract team, relay_url, relay_auth, and peers from parsed TOML data."""
    team: tuple[str, ...] = ()
    relay_url: str | None = None
    relay_auth: RelayAuth | None = None
    peers: tuple[str, ...] = ()

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

    peers_section: object = raw.get("peers")
    if isinstance(peers_section, dict):
        section = cast("dict[str, object]", peers_section)
        repos: object = section.get("repos", [])
        if isinstance(repos, list):
            items_p = cast("list[object]", repos)
            peers = tuple(
                sanitize_repo_name(r) for r in items_p if isinstance(r, str) and r
            )

    return team, relay_url, relay_auth, peers


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
    # Handle empty array: [] → ["github-actions"] (no leading comma)
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
    peers: tuple[str, ...] = ()
    if repo_root is not None:
        raw = load_biff_file(repo_root)
        team, relay_url, relay_auth, peers = extract_biff_fields(raw)

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
        peers=peers,
    )
    return ResolvedConfig(config=config, data_dir=data_dir, repo_root=repo_root)
