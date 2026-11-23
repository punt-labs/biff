"""Relay configuration tool -- ``biff_relay(url, auth, local, relay_type)``.

Chooses the relay (``local``, ``demo`` or ``nats``) and writes it, with an
explicit ``type``, to ``.punt-labs/biff/config.yaml`` or ``config.local.yaml``.
Requires a Claude Code restart for the new relay to take effect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.config import (
    ensure_gitignore_yaml,
    load_yaml_config,
    load_yaml_local,
    relay_section_to_write,
    write_yaml_config,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState


def _target(*, local: bool) -> str:
    """Return the config file the relay is written to."""
    return "config.local.yaml" if local else "config.yaml"


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the biff relay configuration tool."""

    @mcp.tool(
        name="biff_relay",
        description=(
            "Choose the relay for biff: local (this machine, the default), "
            "demo (the shared public relay) or nats (your own server, with a url). "
            "Writes config; restart Claude Code for the change to take effect. "
            "Use local=true to write to config.local.yaml instead."
        ),
    )
    async def biff_relay(
        url: str = "",
        auth: str = "",
        local: bool = False,  # noqa: FBT001, FBT002
        relay_type: str = "",
    ) -> str:
        """Configure the relay for subsequent Claude Code sessions.

        Use this to switch relays; restart Claude Code after saving.
        Shared config is tracked, so authentication can only be saved locally.
        The relay is always written with an explicit ``type``.

        Args:
            url: A NATS URL starting tls://, nats://, ws://, or wss://.
            auth: Optional path to a credentials file; requires local=True.
            local: Write config.local.yaml rather than the shared config.yaml.
            relay_type: local, demo or nats; a url alone means nats.

        Returns:
            A save confirmation and restart reminder, or an error for an invalid
            type or scheme, shared credentials, or a missing repository.

        Raises:
            OSError: Configuration or gitignore cannot be written.
            SystemExit: The existing shared YAML configuration is malformed.
        """
        repo_root = state.repo_root
        if repo_root is None:
            return "Error: not in a git repository."

        section = relay_section_to_write(url, auth, relay_type)
        if isinstance(section, str):
            return section

        # Reject auth in shared config -- credentials belong in
        # config.local.yaml (gitignored) to prevent accidental commits.
        if auth and not local:
            return (
                "Error: auth credentials must go in config.local.yaml "
                "(pass local=true). Shared config.yaml is tracked in git."
            )

        # Build a fresh relay section -- changing the relay invalidates prior
        # auth in the SAME file. A relay chosen in config.local.yaml wins over
        # config.yaml (per file, not key by key); clear it separately if needed.
        existing = load_yaml_local(repo_root) if local else load_yaml_config(repo_root)
        existing["relay"] = section
        write_yaml_config(repo_root, existing, local=local)
        # Keep config.local.yaml out of git once .punt-labs/biff/ exists.
        ensure_gitignore_yaml(repo_root)
        return (
            f"Relay set to {section['type']} in {_target(local=local)}. "
            "Restart Claude Code for the new relay to take effect."
        )
