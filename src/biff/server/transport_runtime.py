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
from biff.server.app import create_server
from biff.server.caller_policy import CallerPolicy
from biff.server.launch import McpLaunch

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState

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
    _user: str | None
    _transport: str

    def create_server(self) -> FastMCP[ServerState]:
        """Create unbound resources with the transport's default caller policy."""
        launch = McpLaunch(
            self._resolved, CallerPolicy.for_launch(self._user, self._transport)
        )
        return create_server(launch.state(), default_caller=launch.caller)


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
