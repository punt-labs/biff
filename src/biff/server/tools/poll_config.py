"""Poll interval configuration tools — ``set_poll_interval`` / ``get_poll_status``.

Allows users to adjust the background polling frequency at runtime
and persist the setting to ``config.local.yaml``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from biff.config import ensure_gitignore_yaml, load_yaml_local, write_yaml_config
from biff.nats_relay import NatsRelay

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState

_INTERVAL_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(s|m)$")


def _parse_interval(value: str) -> float | None:
    """Parse an interval string to seconds, or ``None`` for disable.

    Accepts any ``{N}s`` or ``{N}m`` format (e.g. ``"2s"``, ``"5m"``,
    ``"30s"``), or ``"n"`` to disable.
    """
    value = value.strip().lower()
    if value == "n":
        return None
    m = _INTERVAL_RE.match(value)
    if m is None:
        return -1.0  # sentinel for invalid
    amount = float(m.group(1))
    if amount <= 0:
        return -1.0  # 0s/0m is invalid — use "n" to disable
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
            "Accepts {N}s or {N}m format (e.g. 2s, 30s, 5m), or n (disable). "
            "Persisted to config. Restart required to take effect."
        ),
    )
    async def set_poll_interval(interval: str) -> str:
        """Persist the poll interval to config.local.yaml. Restart required."""
        parsed = _parse_interval(interval)
        if parsed is not None and parsed < 0:
            return (
                f"Invalid interval: {interval}. "
                "Use {N}s or {N}m format (e.g. 2s, 5m), or n to disable."
            )

        repo_root = state.repo_root
        if repo_root is not None:
            existing = load_yaml_local(repo_root)
            if parsed is None:
                existing["poll_interval"] = 0
            else:
                existing["poll_interval"] = parsed
            write_yaml_config(repo_root, existing, local=True)
            # config.local.yaml is per-user; keep it out of git even when a
            # user only ever ran `biff enable` (which no longer touches the
            # gitignore) and then set a poll interval.
            ensure_gitignore_yaml(repo_root)

        if parsed is None:
            return (
                "Polling disabled. Restart Claude Code for the change to take effect."
            )

        return (
            f"Poll interval set to {interval} ({parsed}s). "
            "Restart Claude Code for the change to take effect."
        )

    @mcp.tool(
        name="get_poll_status",
        description="Show the current poll interval and whether polling is active.",
    )
    async def get_poll_status() -> str:
        """Return current polling configuration.

        Appends a cumulative relay-timeout count when the relay is NATS-backed
        and at least one request has been attempted. A silent client-side
        retry (see the ``/biff:read`` and ``/biff:poll`` command prompts)
        would otherwise make relay timeouts unmeasurable to a caller — this
        line is the server-side record a caller or operator can check instead
        of relying on which timeouts happened to be noticed.
        """
        interval = state.config.poll_interval
        if interval <= 0:
            status = "Polling: disabled"
        else:
            if interval >= 60 and interval % 60 == 0:
                display = f"{interval / 60:g}m"
            else:
                display = f"{interval:g}s"
            status = f"Polling: active, interval={display} ({interval:g}s)"

        relay = state.relay
        if isinstance(relay, NatsRelay) and relay.total_attempts > 0:
            status += (
                f"\nNATS relay: {relay.total_timeouts} timeout(s) / "
                f"{relay.total_attempts} request(s) since server start"
            )
        return status
