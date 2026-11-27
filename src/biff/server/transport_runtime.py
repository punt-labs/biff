"""Caller-aware MCP construction and private transport diagnostics."""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING

from uvicorn.config import LOGGING_CONFIG

from biff.config import ResolvedConfig
from biff.server.mcp_assembly import McpAssembly

if TYPE_CHECKING:
    from biff.server.channel_server import ChannelServer
    from biff.server.entrypoints import McpServerOptions
    from biff.server.state import RepoToggle, ServerState

_logger = logging.getLogger(__name__)
_EVENTS = {
    "Shutting down": "HTTP MCP shutdown outcome=started",
    "Finished server process [%d]": "HTTP MCP shutdown outcome=completed",
    "stdio transport returned": "stdio MCP shutdown outcome=completed",
}


@dataclass(frozen=True, slots=True)
class McpServerBuilder:
    """Bind transport launch policy to the resolved process configuration."""

    _resolved: ResolvedConfig
    _options: McpServerOptions
    _toggle: RepoToggle

    def create_server(self) -> ChannelServer[ServerState]:
        """Create unbound resources with the transport's default caller policy."""
        options = self._options
        overrides = (options.data_dir, options.relay_url)
        return McpAssembly(
            self._resolved, options.user, options.transport, self._toggle, overrides
        ).server()


class McpLifecycleHandler(logging.Handler):
    """Translate transport events without copying paths or payloads."""

    @classmethod
    @contextmanager
    def stdio(cls) -> Generator[None]:
        """Report completion through the event allowlist only on normal return."""
        _logger.info("Starting Biff MCP server (stdio)")
        yield
        cls().emit(logging.makeLogRecord({"msg": "stdio transport returned"}))

    def emit(self, record: logging.LogRecord) -> None:
        """Translate allowlisted event templates, never their arguments."""
        if isinstance(record.msg, str) and (event := _EVENTS.get(record.msg)):
            _logger.info(event)

    @classmethod
    def configuration(cls) -> dict[str, object]:
        """Extend uvicorn's configuration without replacing biff's root sinks."""
        config = deepcopy(LOGGING_CONFIG)
        config["handlers"]["biff"] = {"()": cls}
        config["loggers"]["uvicorn.error"] = {"handlers": ["biff"], "level": "INFO"}
        return config
