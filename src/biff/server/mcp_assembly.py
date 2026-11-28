"""Build the MCP server, answering its read-only tools from biffd when asked to."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from biff.client.environment import ClientEnvironment, UnsupportedOverrideError
from biff.config import ResolvedConfig
from biff.mcp_transport import McpTransport
from biff.repo_transport import RepoTransport
from biff.server.app import create_server
from biff.server.caller_policy import CallerPolicy
from biff.server.daemon_channel import DaemonChannelFeed
from biff.server.daemon_channel_tools import DaemonChannelAck
from biff.server.daemon_talk_tools import DaemonTalkTools
from biff.server.daemon_tools import DaemonReadTools
from biff.server.daemon_write_tools import DaemonWriteTools
from biff.server.launch import McpLaunch
from biff.server.mcp_link import McpLink

if TYPE_CHECKING:
    from collections.abc import Mapping

    from biff.server.channel_server import ChannelServer
    from biff.server.state import RepoToggle, ServerState

__all__ = ["McpAssembly"]

_FAMILIES: Final = (DaemonReadTools, DaemonWriteTools, DaemonTalkTools)
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
    def uses_biffd(
        transport: str, repo_root: Path | None, environ: Mapping[str, str]
    ) -> bool:
        """Return whether the read tools ask biffd.

        Only the stdio server does: the HTTP server (``biff serve``) has no
        caller identity of its own to give biffd, so it keeps its relay.
        """
        choice = RepoTransport.of(repo_root, environ)
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
        if asks_biffd:
            try:
                ClientEnvironment.require_no_relay_overrides(os.environ)
            except UnsupportedOverrideError as exc:
                # Refused where it is seen: every channel subscription would fail
                # and the model is told not to poll.
                raise SystemExit(str(exc)) from exc
        state = McpLaunch(self.resolved, self.toggle).state()
        link = None
        if asks_biffd:
            link = McpLink.for_host(user=self.resolved.config.user, cwd=str(Path.cwd()))
            self.feed_channel_from(link, state)
        caller = CallerPolicy.for_launch(self.user, self.transport)
        server = create_server(state, default_caller=caller)
        if link is not None:
            self.answer_from(link, server, state)
        return server

    @staticmethod
    def feed_channel_from(link: McpLink, state: ServerState) -> DaemonChannelFeed:
        """Have *state*'s channel events come from biffd's subscription.

        The emitter is replaced before the server is created, so the channel
        loop serves the one that reads biffd; the feed follows the session the
        server binds to.
        """
        feed = DaemonChannelFeed(link=link, check=state.channel_check)
        object.__setattr__(state, "emitter", feed.emitter)
        object.__setattr__(state, "session_bound", feed.bind)
        return feed

    @staticmethod
    def answer_from(
        link: McpLink, server: ChannelServer[ServerState], state: ServerState
    ) -> None:
        """Have *server* ask biffd, through *link*, for what its tools say."""
        for family in _FAMILIES:
            for name in family.NAMES:
                server.local_provider.remove_tool(name)
            family(server, state, link).register()
        # ``channel_ack`` exists only while channel mode serves: biffd's answers it.
        server.attach_channel_tools(DaemonChannelAck(server, state, link).register)
