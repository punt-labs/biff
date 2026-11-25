"""Poll interval configuration tools — ``set_poll_interval`` / ``get_poll_status``.

Lets users record the polling cadence that ``/biff:read`` schedules and
persist it to ``config.local.yaml``.  The server runs no timer for it
(DES-087).
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
_INVALID = -1.0  # _parse_interval's answer for text that is not an interval


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
        return _INVALID
    amount = float(m.group(1))
    if amount <= 0:
        return _INVALID  # 0s/0m is invalid — use "n" to disable
    return amount * 60 if m.group(2) == "m" else amount


_SET_POLL_INTERVAL_DESCRIPTION = (
    "Record the polling cadence the user asked for. Biff's server runs no "
    "timer for it: messages, walls and talk arrive by NATS push or, on the "
    "local relay, by a file watch, whatever this value is, and an explicit "
    "read recomputes the unread count. The cadence is how often /biff:read "
    "schedules its own check. Accepts {N}s or {N}m format (e.g. 2s, 30s, "
    "5m), or n (polling off). Persisted to config. Restart required to "
    "take effect."
)

_DISABLE_RESPONSE = (
    "Polling is off. Biff's server runs no timer either way: messages, "
    "walls and talk still arrive by NATS push or, on the local relay, by "
    "a file watch. Restart Claude Code for the change to take effect."
)


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register poll configuration tools."""

    @mcp.tool(name="set_poll_interval", description=_SET_POLL_INTERVAL_DESCRIPTION)
    async def set_poll_interval(interval: str) -> str:
        """Save the polling cadence for the next server start.

        Args:
            interval: A positive duration such as '2s' or '5m', or 'n' to turn
                polling off. Arrivals do not depend on it: the server is woken
                by NATS push or by the local relay's file watch.

        Returns:
            A confirmation and restart reminder, or an invalid-interval notice.
            Without a repository the response is returned but no file is saved.

        Raises:
            OSError: The local configuration or gitignore cannot be written.
        """
        parsed = _parse_interval(interval)
        if parsed == _INVALID:
            return (
                f"Invalid interval: {interval}. "
                "Use {N}s or {N}m format (e.g. 2s, 5m), or n to disable."
            )

        repo_root = state.repo_root
        if repo_root is not None:
            existing = load_yaml_local(repo_root)
            existing["poll_interval"] = parsed or 0  # never 0.0: that is rejected above
            write_yaml_config(repo_root, existing, local=True)
            # config.local.yaml is per-user; keep it out of git even when a
            # user has not run interactive configuration.
            ensure_gitignore_yaml(repo_root)

        if state.unread_path is not None:  # the nudge hook speaks only while "True"
            state.unread_path.with_suffix(".polling").write_text(
                str(parsed is not None)
            )
        if parsed is None:
            return _DISABLE_RESPONSE

        return (
            f"Poll interval set to {interval} ({parsed}s). "
            "Restart Claude Code for the change to take effect."
        )

    @mcp.tool(
        name="get_poll_status",
        description="Show the current poll interval and whether polling is active.",
    )
    async def get_poll_status() -> str:
        """Inspect the current polling cadence and relay timeout counters.

        Call without arguments to diagnose the running server, not pending config
        changes. Read in-memory state only; this does not issue a relay request.

        Returns:
            Whether periodic polling is enabled and its interval. For NATS, append
            cumulative timeouts and attempted requests when any have been made.
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
