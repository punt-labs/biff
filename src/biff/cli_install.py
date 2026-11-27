"""The machine-scope steps of ``biff install`` and ``biff uninstall``."""

from __future__ import annotations

from typing import final

import typer

from biff import _stdlib
from biff.daemon.lifecycle import BiffdService

__all__ = ["MachineScope"]


@final
class MachineScope:
    """What install and uninstall do for the machine apart from the plugin."""

    @staticmethod
    def provision() -> bool:
        """Create biff's home tree and install biffd's service.

        Returns whether the service install failed; a host with no service
        manager is not a failure, because biffd then starts on demand.

        Raises:
            typer.Exit: The home tree could not be created.
        """
        try:
            _stdlib.provision_home()
        except (OSError, RuntimeError) as exc:
            typer.echo(f"Could not create biff's home directory: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        outcome = BiffdService.for_host().install()
        failed = outcome.status == "failed"
        typer.echo(outcome.message, err=failed)
        return failed

    @staticmethod
    def deprovision() -> bool:
        """Remove biffd's service; return whether that failed.

        A host with no service manager has nothing to remove and prints nothing.
        """
        outcome = BiffdService.for_host().uninstall()
        failed = outcome.status == "failed"
        if outcome.status != "no_manager":
            typer.echo(outcome.message, err=failed)
        return failed
