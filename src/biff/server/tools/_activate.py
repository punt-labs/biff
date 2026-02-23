"""Lazy activation — auto-enable biff on first tool use while dormant.

When biff starts in dormant mode (no ``.biff.local`` with ``enabled = true``),
calling any tool is treated as intent to use biff.  ``lazy_activate`` writes
the activation config to disk and returns a restart message.  The caller
returns the message early — the actual tool logic is skipped until the next
session when biff starts connected.

The ``biff`` toggle tool is the one exception: it handles its own
enable/disable logic and must NOT call ``lazy_activate``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.config import (
    DEMO_RELAY_URL,
    build_biff_toml,
    ensure_gitignore,
    write_biff_local,
)

if TYPE_CHECKING:
    from biff.server.state import ServerState


def lazy_activate(state: ServerState) -> str | None:
    """Auto-enable biff on first tool use while dormant.

    Returns an activation message if the server was dormant (caller
    should return this string).  Returns ``None`` if already active.
    """
    if not state.dormant:
        return None

    repo_root = state.repo_root
    if repo_root is None:
        return "biff: not in a git repository."

    # Create .biff if missing (non-interactive, using current config)
    if not (repo_root / ".biff").exists():
        from biff.relay import atomic_write  # noqa: PLC0415

        relay_url = state.config.relay_url or DEMO_RELAY_URL
        content = build_biff_toml(list(state.config.team), relay_url)
        atomic_write(repo_root / ".biff", content)

    write_biff_local(repo_root, enabled=True)
    ensure_gitignore(repo_root)

    return "biff enabled. Restart Claude Code to connect."
