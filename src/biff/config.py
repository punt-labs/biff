"""Configuration discovery and loading.

Finds the ``.biff`` TOML file at the git repo root, resolves user
identity from ``git config biff.user``, and computes the shared data
directory.

Config file format (``.biff``)::

    [team]
    members = ["kai", "eric", "priya"]

    [relay]
    url = "nats://localhost:4222"

Data directory layout::

    {prefix}/biff/{repo-name}/
        inbox-kai.jsonl
        inbox-eric.jsonl
        sessions.json
"""

from __future__ import annotations

import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from biff.models import BiffConfig

_DEFAULT_PREFIX = Path("/tmp")  # noqa: S108


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


def get_git_user() -> str | None:
    """Read ``git config biff.user``, returning *None* if unset."""
    try:
        result = subprocess.run(
            ["git", "config", "biff.user"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        value = result.stdout.strip()
        return value if result.returncode == 0 and value else None
    except FileNotFoundError:
        return None


def compute_data_dir(repo_root: Path, prefix: Path) -> Path:
    """Compute data directory: ``{prefix}/biff/{repo_name}/``."""
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


def _extract_biff_fields(
    raw: dict[str, object],
) -> tuple[tuple[str, ...], str | None]:
    """Extract team and relay_url from parsed TOML data."""
    team: tuple[str, ...] = ()
    relay_url: str | None = None

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

    return team, relay_url


def load_config(
    *,
    user_override: str | None = None,
    data_dir_override: Path | None = None,
    prefix: Path = _DEFAULT_PREFIX,
    start: Path | None = None,
) -> ResolvedConfig:
    """Discover and resolve all configuration.

    Resolution order:

    1. CLI overrides (``user_override``, ``data_dir_override``) take precedence.
    2. ``.biff`` TOML for team roster and relay URL.
    3. ``git config biff.user`` for identity.
    4. Data dir computed from ``{prefix}/biff/{repo_name}/``.

    Raises :class:`SystemExit` if user or data dir cannot be resolved.
    """
    repo_root = find_git_root(start)

    # Parse .biff file
    team: tuple[str, ...] = ()
    relay_url: str | None = None
    if repo_root is not None:
        raw = load_biff_file(repo_root)
        team, relay_url = _extract_biff_fields(raw)

    # Resolve user
    user = user_override or get_git_user()
    if user is None:
        msg = (
            "No user configured. Set via: git config biff.user <handle>"
            " or pass --user <handle>"
        )
        raise SystemExit(msg)

    # Resolve data dir
    if data_dir_override is not None:
        data_dir = data_dir_override
    elif repo_root is not None:
        data_dir = compute_data_dir(repo_root, prefix)
    else:
        msg = (
            "Cannot determine data directory: not in a git repo"
            " and no --data-dir specified"
        )
        raise SystemExit(msg)

    config = BiffConfig(user=user, relay_url=relay_url, team=team)
    return ResolvedConfig(config=config, data_dir=data_dir, repo_root=repo_root)
