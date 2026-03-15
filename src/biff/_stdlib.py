"""Stdlib-only helpers for lightweight hook execution.

This module contains functions extracted from heavier modules
(``config``, ``server.tools.plan``) that only need stdlib imports.
Hook entry points import from here to avoid pulling in pydantic,
nats, typer, and the full server dependency tree.

Every function in this module MUST use only stdlib imports.
Adding a third-party import here defeats the entire purpose.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path
from typing import cast

# ── Git helpers ──────────────────────────────────────────────────────


def find_git_root(start: Path | None = None) -> Path | None:
    """Walk up from *start* (default: cwd) to find the git repo root."""
    path = (start or Path.cwd()).resolve()
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            return parent
    return None


_SLUG_SCP_RE = re.compile(r"^[^@]+@[^:]+:(.+?)(?:\.git)?$")
_SLUG_URL_RE = re.compile(r"^(?:https?|ssh)://[^/]+(?::\d+)?/(.+?)(?:\.git)?$")


def _parse_repo_slug(url: str) -> str | None:
    """Extract ``owner/repo`` from a git remote URL.

    Supports scp-style SSH (``git@host:owner/repo``), scheme-based SSH
    (``ssh://git@host/owner/repo``, with optional port), and HTTPS.
    Returns ``None`` for URLs that don't match or have nested paths
    (e.g. ``gitlab.com/group/sub/repo``).
    """
    for pattern in (_SLUG_SCP_RE, _SLUG_URL_RE):
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


def display_repo_name(name: str) -> str:
    """Convert a sanitized repo name back to display form.

    Reverses the first ``__`` → ``/`` from :func:`sanitize_repo_name`
    so ``"punt-labs__biff"`` displays as ``"punt-labs/biff"``.
    Returns *name* unchanged when no ``__`` is present.

    Note: the round-trip is lossy — a repo name that legitimately
    contains ``__`` would be misinterpreted as an owner/repo separator.
    """
    return name.replace("__", "/", 1) if name else name


def sanitize_repo_name(name: str) -> str:
    """Sanitize a repo name or slug for use in NATS resource names.

    NATS bucket names allow ASCII alphanumeric, dash, and underscore
    only.  Subject dots are level separators; wildcards (``*``, ``>``)
    are reserved.  Slashes become double underscores (``__``) to mark
    the owner/repo boundary without colliding with underscores in repo
    names; dots become dashes; spaces become dashes; non-ASCII and
    remaining special characters are stripped.

    Raises ``SystemExit`` if the result is empty — a repo name that
    sanitizes to nothing would silently share a NATS namespace with
    other unusable names, causing the exact collision this function
    exists to prevent.
    """
    clean = name.replace("/", "__").replace(".", "-").replace(" ", "-")
    sanitized = "".join(c for c in clean if (c.isascii() and c.isalnum()) or c in "-_")
    if not sanitized:
        raise SystemExit(
            f"Repo name {name!r} contains no usable characters after sanitization.\n"
            "Rename the directory to include ASCII letters or digits."
        )
    return sanitized


# ── Config helpers ───────────────────────────────────────────────────


def load_biff_local(repo_root: Path) -> dict[str, object]:
    """Parse ``.biff.local`` TOML at *repo_root*, or return ``{}`` if missing."""
    path = repo_root / ".biff.local"
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError:
        return {}


def is_enabled(repo_root: Path | None) -> bool:
    """True only if ``.biff.local`` exists with ``enabled = true``.

    Returns ``False`` if: *repo_root* is ``None``, no ``.biff`` file,
    no ``.biff.local`` file, or ``enabled`` is not ``true``.
    """
    if repo_root is None:
        return False
    if not (repo_root / ".biff").exists():
        return False
    local = load_biff_local(repo_root)
    return local.get("enabled") is True


# ── Bead helpers ─────────────────────────────────────────────────────

_BEAD_ID_RE = re.compile(r"^[a-z]+-[a-z0-9]{2,4}$")


def expand_bead_id(message: str) -> str:
    """Expand a bare bead ID to ``<id>: <title>`` if possible.

    If the message matches the bead ID pattern and ``bd`` can resolve
    the title, returns the expanded form.  Otherwise returns the
    original message unchanged.
    """
    if not _BEAD_ID_RE.match(message):
        return message
    try:
        result = subprocess.run(  # noqa: S603
            ["bd", "show", message, "--json", "-q"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode != 0:
            return message
        data = json.loads(result.stdout)
        if isinstance(data, list) and data:
            items = cast("list[object]", data)
            first = items[0]
            if isinstance(first, dict):
                rec = cast("dict[str, object]", first)
                title = rec.get("title", "")
                if isinstance(title, str) and title:
                    return f"{message}: {title}"
    except (FileNotFoundError, json.JSONDecodeError, TimeoutError, OSError):
        pass
    return message


# ── Data directory ──────────────────────────────────────────────────


def biff_data_dir() -> Path:
    """Root data directory: ``~/.punt-labs/biff/``."""
    return Path.home() / ".punt-labs" / "biff"


BIFF_DATA_DIR = biff_data_dir()

# ── Session lifecycle helpers ────────────────────────────────────────


def active_dir() -> Path:
    """Active session directory: ``~/.punt-labs/biff/active/``."""
    return biff_data_dir() / "active"


def remove_active_session(session_key: str) -> None:
    """Remove the active session marker on shutdown."""
    safe = session_key.replace(":", "-")
    (active_dir() / safe).unlink(missing_ok=True)


def sentinel_dir(repo_name: str) -> Path:
    """Sentinel directory for a repo: ``~/.punt-labs/biff/sentinels/{repo_name}/``."""
    return biff_data_dir() / "sentinels" / repo_name


# ── Lux helpers ─────────────────────────────────────────────────────


def is_lux_enabled(repo_root: Path | None = None) -> bool:
    """Check whether lux display mode is enabled.

    Reads ``.lux/config.md`` YAML frontmatter for ``display: "y"``.
    Returns ``False`` if the file is absent, malformed, or display is off.

    Uses only stdlib — safe for hook entry points and lightweight callers.
    """
    if repo_root is None:
        repo_root = find_git_root()
    if repo_root is None:
        return False
    config = repo_root / ".lux" / "config.md"
    if not config.is_file():
        return False
    try:
        text = config.read_text()
        # Parse YAML frontmatter: ---\ndisplay: "y"\n---
        if not text.startswith("---"):
            return False
        end = text.find("---", 3)
        if end == -1:
            return False
        frontmatter = text[3:end]
        for line in frontmatter.splitlines():
            stripped = line.strip()
            if stripped.startswith("display:"):
                value = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                return value == "y"
    except OSError:
        pass
    return False
