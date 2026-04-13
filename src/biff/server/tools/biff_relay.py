"""Relay configuration tool -- ``biff_relay(url, ...)``.

Sets the relay URL (and optionally auth credentials), writes to
``.punt-labs/biff/config.yaml`` or ``config.local.yaml``.
Requires a Claude Code restart for the new relay to take effect.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from biff.config import (
    ensure_gitignore_yaml,
    load_yaml_config,
    write_yaml_config,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState

logger = logging.getLogger(__name__)

_VALID_SCHEMES = ("tls://", "nats://", "ws://", "wss://")


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the biff relay configuration tool."""

    @mcp.tool(
        name="biff_relay",
        description=(
            "Set the relay URL for biff. "
            "Writes config and signals live reconnect. "
            "Use local=true to write to config.local.yaml instead."
        ),
    )
    async def biff_relay(
        url: str,
        auth: str = "",
        local: bool = False,  # noqa: FBT001, FBT002
    ) -> str:
        """Configure the relay URL and trigger reconnect.

        Parameters
        ----------
        url:
            Relay URL (must start with ``tls://``, ``nats://``,
            ``ws://``, or ``wss://``).
        auth:
            Optional path to a credentials file.
        local:
            Write to ``config.local.yaml`` instead of ``config.yaml``.
        """
        repo_root = state.repo_root
        if repo_root is None:
            return "error: not in a git repository"

        # Validate URL scheme
        if not any(url.startswith(scheme) for scheme in _VALID_SCHEMES):
            schemes = ", ".join(_VALID_SCHEMES)
            return f"error: invalid relay URL scheme, expected one of {schemes}"

        if local:
            from biff.config import load_yaml_local  # noqa: PLC0415

            existing = load_yaml_local(repo_root)
            relay_section = existing.get("relay", {})
            if not isinstance(relay_section, dict):
                relay_section = {}
            relay_section["url"] = url
            if auth:
                relay_section["auth"] = {"credentials": auth}
            existing["relay"] = relay_section
            write_yaml_config(repo_root, existing, local=True)
            ensure_gitignore_yaml(repo_root)
            target = "config.local.yaml"
        else:
            existing = load_yaml_config(repo_root)
            relay_section = existing.get("relay", {})
            if not isinstance(relay_section, dict):
                relay_section = {}
            relay_section["url"] = url
            if auth:
                relay_section["auth"] = {"credentials": auth}
            existing["relay"] = relay_section
            write_yaml_config(repo_root, existing, local=False)
            target = "config.yaml"

        return (
            f"Relay URL set to {url} in {target}. "
            "Restart Claude Code for the new relay to take effect."
        )
