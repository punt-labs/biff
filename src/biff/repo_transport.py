"""The MCP transport a repository's settings choose."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from biff.config import load_yaml_config, load_yaml_local, merge_config
from biff.mcp_transport import McpTransport

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

__all__ = ["RepoTransport"]


@final
class RepoTransport:
    """Reads ``mcp.transport`` from a repository's config files and the environment."""

    __slots__ = ()

    @staticmethod
    def of(repo_root: Path | None, environ: Mapping[str, str]) -> McpTransport:
        """Return the transport the settings of *repo_root* and *environ* choose.

        Raises:
            SystemExit: A setting names neither transport, or a config file is
                malformed.
        """
        merged: dict[str, object] = {}
        if repo_root is not None:
            merged = merge_config(
                load_yaml_config(repo_root), load_yaml_local(repo_root)
            )
        return McpTransport.from_settings(merged, environ)
