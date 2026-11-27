"""The root ``biff`` Typer app with its command groups attached."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from biff.daemon.app import daemon_app
from biff.plugins.claude_code._hook_cli import HookAwareCli
from biff.server.entrypoints import McpServerOptions

if TYPE_CHECKING:
    import typer

__all__ = ["CliApp"]


@final
class CliApp:
    """Assembles the root app; the one place that names the attached groups."""

    @staticmethod
    def build() -> typer.Typer:
        """Return the root app with the server commands and ``daemon`` group."""
        app = HookAwareCli.create_app()
        app.command()(McpServerOptions.serve)
        app.command()(McpServerOptions.mcp)
        app.add_typer(daemon_app, name="daemon")
        return app
