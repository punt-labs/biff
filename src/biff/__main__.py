"""Biff CLI entry point.

Provides ``biff serve`` for running the MCP server with stdio or HTTP transport.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from biff.models import BiffConfig
from biff.server.app import create_server
from biff.server.state import create_state

app = typer.Typer(help="Biff: the dog that barked when messages arrived.")

_DEFAULT_DATA_DIR = Path.home() / ".biff" / "data"


@app.command()
def serve(
    user: Annotated[str, typer.Option(help="Your username.")],
    data_dir: Annotated[
        Path, typer.Option(help="Data directory for messages and sessions.")
    ] = _DEFAULT_DATA_DIR,
    transport: Annotated[
        str, typer.Option(help="Transport: 'stdio' or 'http'.")
    ] = "stdio",
    host: Annotated[
        str, typer.Option(help="HTTP host (http transport only).")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="HTTP port (http transport only).")] = 8419,
) -> None:
    """Start the biff MCP server."""
    config = BiffConfig(user=user)
    state = create_state(config, data_dir)
    mcp = create_server(state)

    if transport == "http":
        print(f"Starting biff MCP server on http://{host}:{port}")
        mcp.run(transport="http", host=host, port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    app()
