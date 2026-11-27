"""The ``biff daemon`` command group and the ``biffd`` executable."""

from __future__ import annotations

import typer

from biff.daemon.cli import DaemonCommands
from biff.daemon.service_cli import ServiceCommands

__all__ = ["biffd_app", "daemon_app"]

daemon_app = typer.Typer(
    help="Run and manage the per-user biff daemon.",
    no_args_is_help=True,
)
daemon_app.command("serve")(DaemonCommands.serve)
daemon_app.command("install")(ServiceCommands.install)
daemon_app.command("uninstall")(ServiceCommands.uninstall)
daemon_app.command("start")(ServiceCommands.start)
daemon_app.command("stop")(ServiceCommands.stop)
daemon_app.command("restart")(ServiceCommands.restart)
daemon_app.command("status")(ServiceCommands.status)

# The executable a service unit runs: ``biff daemon serve`` without the prefix.
biffd_app = typer.Typer(add_completion=False)
biffd_app.command()(DaemonCommands.serve)
