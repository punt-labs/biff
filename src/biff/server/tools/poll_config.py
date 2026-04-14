"""Poll interval configuration tools — ``set_poll_interval`` / ``get_poll_status``.

Allows users to adjust the background polling frequency at runtime
and persist the setting to ``config.local.yaml``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from biff.config import load_yaml_local, write_yaml_config

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState

_INTERVAL_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(s|m)$")

_VALID_PRESETS = frozenset({"2s", "5s", "10s", "30s", "1m", "2m", "5m", "n"})


def _parse_interval(value: str) -> float | None:
    """Parse an interval string to seconds, or ``None`` for disable.

    Accepts: ``"2s"``, ``"5s"``, ``"10s"``, ``"30s"``, ``"1m"``,
    ``"2m"``, ``"5m"``, ``"n"`` (disable).
    """
    value = value.strip().lower()
    if value == "n":
        return None
    m = _INTERVAL_RE.match(value)
    if m is None:
        return -1.0  # sentinel for invalid
    amount = float(m.group(1))
    unit = m.group(2)
    if unit == "m":
        return amount * 60
    return amount


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register poll configuration tools."""

    @mcp.tool(
        name="set_poll_interval",
        description=(
            "Set the background polling interval. "
            "Accepts: 2s, 5s, 10s, 30s, 1m, 2m, 5m, or n (disable)."
        ),
    )
    async def set_poll_interval(interval: str) -> str:
        """Update the poll interval in-memory and persist to config.local.yaml."""
        parsed = _parse_interval(interval)
        if parsed is not None and parsed < 0:
            return (
                f"Invalid interval: {interval}. "
                f"Use one of: {', '.join(sorted(_VALID_PRESETS))}."
            )

        repo_root = state.repo_root
        if repo_root is not None:
            existing = load_yaml_local(repo_root)
            if parsed is None:
                existing["poll_interval"] = 0
            else:
                existing["poll_interval"] = parsed
            write_yaml_config(repo_root, existing, local=True)

        if parsed is None:
            # Update in-memory config via model_copy (frozen model).
            object.__setattr__(state.config, "poll_interval", 0.0)
            return "Polling disabled."

        object.__setattr__(state.config, "poll_interval", parsed)
        return f"Poll interval set to {interval} ({parsed}s)."

    @mcp.tool(
        name="get_poll_status",
        description="Show the current poll interval and whether polling is active.",
    )
    async def get_poll_status() -> str:
        """Return current polling configuration."""
        interval = state.config.poll_interval
        if interval <= 0:
            return "Polling: disabled"
        display = f"{interval / 60:.0f}m" if interval >= 60 else f"{interval:.0f}s"
        return f"Polling: active, interval={display} ({interval}s)"
