"""Build the MCP server, answering its read-only tools from biffd when asked to."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from biff.config import ResolvedConfig, load_yaml_config, load_yaml_local, merge_config
from biff.mcp_transport import McpTransport
from biff.server.app import create_server
from biff.server.caller_policy import CallerPolicy
from biff.server.daemon_tools import DaemonReadTools
from biff.server.launch import McpLaunch
from biff.server.mcp_link import McpLink

if TYPE_CHECKING:
    from collections.abc import Mapping

    from biff.server.channel_server import ChannelServer
    from biff.server.state import RepoToggle, ServerState

__all__ = ["McpAssembly"]

_READS: Final = ("who", "finger", "last", "read_messages")
_MOVED: Final = (
    "mcp.transport: daemon cannot be used with --data-dir or --relay-url: biffd "
    "resolves the relay from the repository, so the read tools would read another "
    "relay than the write tools. Drop the option, or set mcp.transport: inprocess"
)


@dataclass(frozen=True, slots=True)
class McpAssembly:
    """The resolved process configuration, and the server it asks for."""

    resolved: ResolvedConfig
    user: str | None
    transport: str
    toggle: RepoToggle
    overrides: tuple[Path | None, str | None]  # --data-dir, --relay-url

    @staticmethod
    def transport_for(
        repo_root: Path | None, environ: Mapping[str, str]
    ) -> McpTransport:
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

    @staticmethod
    def uses_biffd(
        transport: str, repo_root: Path | None, environ: Mapping[str, str]
    ) -> bool:
        """Return whether the read tools ask biffd.

        Only the stdio server does: the HTTP server (``biff serve``) has no
        caller identity of its own to give biffd, so it keeps its relay.
        """
        choice = McpAssembly.transport_for(repo_root, environ)
        return transport == "stdio" and choice is McpTransport.DAEMON

    def server(self) -> ChannelServer[ServerState]:
        """Create the server; with ``mcp.transport: daemon``, biffd answers reads.

        Raises:
            SystemExit: The daemon transport was chosen and an option moves the
                relay, which biffd would not follow.
        """
        asks_biffd = self.uses_biffd(
            self.transport, self.resolved.repo_root, os.environ
        )
        if asks_biffd and any(o is not None for o in self.overrides):
            raise SystemExit(_MOVED)
        state = McpLaunch(self.resolved, self.toggle).state()
        caller = CallerPolicy.for_launch(self.user, self.transport)
        server = create_server(state, default_caller=caller)
        if asks_biffd:
            cwd = str(Path.cwd())
            link = McpLink.for_host(user=self.resolved.config.user, cwd=cwd)
            self.answer_reads_from(link, server, state)
        return server

    @staticmethod
    def answer_reads_from(
        link: McpLink, server: ChannelServer[ServerState], state: ServerState
    ) -> None:
        """Have *server* ask biffd, through *link*, for what its read tools say."""
        for name in _READS:
            server.local_provider.remove_tool(name)
        DaemonReadTools(server, state, link).register()
