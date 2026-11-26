"""The ``biff daemon`` command group."""

from __future__ import annotations

import asyncio
import errno
import os
from typing import Annotated, final

import typer

from biff.daemon.environment import DaemonEnvironment
from biff.daemon.server import BiffDaemon
from biff.run_dir import DEFAULT_PORT, RunDir

__all__ = ["DaemonCommands", "daemon_app"]


@final
class DaemonCommands:
    """The commands of ``biff daemon``."""

    @staticmethod
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
        """Run the daemon in the foreground until interrupted.

        Exits 1 with one line saying what to do when the daemon cannot start:
        the port is taken, or the run directory or token file is not private.
        """
        removed = DaemonEnvironment.scrub(os.environ)
        if removed:
            typer.echo(
                f"biffd: ignoring {', '.join(removed)} from its environment; "
                "each session resolves its own",
                err=True,
            )
        daemon = BiffDaemon(run_dir=RunDir.default(), port=port)
        try:
            asyncio.run(daemon.run_foreground())
        except PermissionError as exc:
            DaemonCommands._fail(str(exc))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                DaemonCommands._fail(
                    f"port {port} is already in use; see what holds it with: "
                    f"lsof -nP -iTCP:{port} -sTCP:LISTEN"
                )
            DaemonCommands._fail(f"cannot listen on 127.0.0.1:{port}: {exc}")
        except ValueError as exc:
            DaemonCommands._fail(str(exc))

    @staticmethod
    def _fail(message: str) -> None:
        """Print *message* as biffd's one-line error and exit 1."""
        typer.echo(f"biffd: {message}", err=True)
        raise typer.Exit(code=1) from None


daemon_app = typer.Typer(
    help="Run the per-user biff daemon.",
    no_args_is_help=True,
)
daemon_app.command("serve")(DaemonCommands.serve)
