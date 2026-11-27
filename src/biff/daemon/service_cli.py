"""The ``biff daemon`` commands that manage biffd's service."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, final

import typer

from biff.daemon.lifecycle import NO_MANAGER_EXIT, STARTING_EXIT, BiffdService

if TYPE_CHECKING:
    from biff.daemon.lifecycle import Outcome

__all__ = ["ServiceCommands"]


@final
class ServiceCommands:
    """Install, remove, control and report on biffd's user service."""

    @staticmethod
    def install() -> None:
        """Install biffd as a user service and start it."""
        ServiceCommands._report(ServiceCommands._service().install())

    @staticmethod
    def uninstall() -> None:
        """Stop and remove biffd's user service."""
        ServiceCommands._report(ServiceCommands._service().uninstall(), idle_ok=True)

    @staticmethod
    def start() -> None:
        """Start the installed service."""
        ServiceCommands._report(ServiceCommands._service().start())

    @staticmethod
    def stop() -> None:
        """Stop the service; it stays installed and a clean stop stays stopped."""
        ServiceCommands._report(ServiceCommands._service().stop())

    @staticmethod
    def restart() -> None:
        """Restart the installed service."""
        ServiceCommands._report(ServiceCommands._service().restart())

    @staticmethod
    def status() -> None:
        """Show whether the service is installed and running, and biffd's health.

        Exits 1 when biffd does not answer ``/health``.
        """
        ServiceCommands._report(ServiceCommands._service().status())

    @staticmethod
    def _service() -> BiffdService:
        """Return the service on this host; a long wait says so on stderr."""
        return BiffdService.for_host(partial(typer.echo, err=True))

    @staticmethod
    def _report(outcome: Outcome, *, idle_ok: bool = False) -> None:
        """Print *outcome* and exit 1 unless it succeeded.

        A host with no service manager has nothing to act on and exits 3, which
        only counts as success when *idle_ok* says an absent service is the goal.
        A manager that refused exits 1.
        """
        unmanaged = outcome.status in ("no_manager", "unreachable") and not idle_ok
        starting = outcome.status == "starting"
        failed = outcome.status == "failed" or unmanaged
        typer.echo(outcome.message, err=failed or starting)
        if failed or starting:
            if starting:
                raise typer.Exit(code=STARTING_EXIT)
            raise typer.Exit(code=NO_MANAGER_EXIT if unmanaged else 1)
