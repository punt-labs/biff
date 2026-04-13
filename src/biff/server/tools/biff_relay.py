"""Relay configuration tool -- ``biff_relay(url, ...)``.

Sets the relay URL (and optionally auth credentials), writes to
``.punt-labs/biff/config.yaml`` or ``config.local.yaml``.
Requires a Claude Code restart for the new relay to take effect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.config import (
    ensure_gitignore_yaml,
    load_yaml_config,
    write_yaml_config,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState

_VALID_SCHEMES = ("tls://", "nats://", "ws://", "wss://")


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the biff relay configuration tool."""

    @mcp.tool(
        name="biff_relay",
        description=(
            "Set the relay URL for biff. "
            "Writes config; restart Claude Code for the change to take effect. "
            "Use local=true to write to config.local.yaml instead."
        ),
    )
    async def biff_relay(
        url: str,
        auth: str = "",
        local: bool = False,  # noqa: FBT001, FBT002
    ) -> str:
        """Configure the relay URL. Restart required.

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

        # Reject auth in shared config — credentials belong in
        # config.local.yaml (gitignored) to prevent accidental commits.
        if auth and not local:
            return (
                "error: auth credentials must go in config.local.yaml "
                "(pass local=true). Shared config.yaml is tracked in git."
            )

        # Build a fresh relay section — changing URL invalidates prior
        # auth (different relay likely needs different credentials).
        # User must pass --auth explicitly to set new credentials.
        relay_section: dict[str, object] = {"url": url}
        if auth:
            relay_section["auth"] = {"credentials": auth}

        if local:
            from biff.config import load_yaml_local  # noqa: PLC0415

            existing = load_yaml_local(repo_root)
            existing["relay"] = relay_section
            write_yaml_config(repo_root, existing, local=True)
            ensure_gitignore_yaml(repo_root)
            target = "config.local.yaml"
        else:
            existing = load_yaml_config(repo_root)
            existing["relay"] = relay_section
            write_yaml_config(repo_root, existing, local=False)
            target = "config.yaml"

        return (
            f"Relay URL set to {url} in {target}. "
            "Restart Claude Code for the new relay to take effect."
        )
