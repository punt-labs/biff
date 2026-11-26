"""The ``biff daemon`` command group."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from biff.daemon.server import BiffDaemon
from biff.run_dir import DEFAULT_PORT, RunDir

__all__ = ["daemon_app"]

daemon_app = typer.Typer(
    help="Run the per-user biff daemon.",
    no_args_is_help=True,
)


@daemon_app.command("serve")
def serve(
    port: Annotated[
        int,
        typer.Option(
            "--port",
            min=0,
            max=65535,
            help="Loopback port to listen on; 0 picks a free one.",
        ),
    ] = DEFAULT_PORT,
) -> None:
    """Run the daemon in the foreground until interrupted."""
    daemon = BiffDaemon(run_dir=RunDir.default(), port=port)
    asyncio.run(daemon.run_foreground())
