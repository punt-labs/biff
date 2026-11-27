"""The ``biff daemon`` command group and the ``biffd`` executable."""

from __future__ import annotations

from typing import final

import typer

from biff.daemon.cli import DaemonCommands
from biff.daemon.service_cli import ServiceCommands

__all__ = ["DaemonApps"]


@final
class DaemonApps:
    """Builds the two ways to reach biffd's commands."""

    @staticmethod
    def group() -> typer.Typer:
        """Return the ``biff daemon`` group: serve, and the service commands."""
        app = typer.Typer(
            help="Run and manage the per-user biff daemon.",
            no_args_is_help=True,
        )
        app.command("serve")(DaemonCommands.serve)
        app.command("install")(ServiceCommands.install)
        app.command("uninstall")(ServiceCommands.uninstall)
        app.command("start")(ServiceCommands.start)
        app.command("stop")(ServiceCommands.stop)
        app.command("restart")(ServiceCommands.restart)
        app.command("status")(ServiceCommands.status)
        return app

    @staticmethod
    def biffd() -> None:
        """Run the daemon in the foreground: the ``biffd`` executable a unit runs."""
        typer.run(DaemonCommands.serve)
