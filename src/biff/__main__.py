"""Biff CLI entry point.

Provides ``biff serve`` and ``biff version``.
"""

from __future__ import annotations

from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Annotated

import click
import typer

from biff.config import load_config
from biff.server.app import create_server
from biff.server.state import create_state

app = typer.Typer(help="Biff: the dog that barked when messages arrived.")


@app.command()
def version() -> None:
    """Print the biff version."""
    print(f"biff {pkg_version('biff-mcp')}")


@app.command()
def serve(
    user: Annotated[
        str | None,
        typer.Option(help="Your username. Auto-detected from 'git config biff.user'."),
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option(help="Data directory. Auto-computed as {prefix}/biff/{repo}."),
    ] = None,
    prefix: Annotated[
        Path,
        typer.Option(help="Base path for data directory (default: /tmp)."),
    ] = Path("/tmp"),  # noqa: S108
    transport: Annotated[
        str,
        typer.Option(
            help="Transport: 'stdio' or 'http'.",
            click_type=click.Choice(["stdio", "http"]),
        ),
    ] = "stdio",
    host: Annotated[
        str, typer.Option(help="HTTP host (http transport only).")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="HTTP port (http transport only).")] = 8419,
) -> None:
    """Start the biff MCP server."""
    resolved = load_config(
        user_override=user,
        data_dir_override=data_dir,
        prefix=prefix,
    )
    state = create_state(
        resolved.config,
        resolved.data_dir,
        unread_path=resolved.data_dir / "unread.json",
    )
    mcp = create_server(state)

    if transport == "http":
        print(f"Starting biff MCP server on http://{host}:{port}")
        mcp.run(transport="http", host=host, port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    app()
