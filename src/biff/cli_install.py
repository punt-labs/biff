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

        Returns whether the service install failed.  A host with no service
        manager is not a failure, because biffd then starts on demand; a manager
        that exists and does not answer is printed to stderr with its reason, and
        also is not, because a container that ships systemctl without running
        systemd is the same case.

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
        typer.echo(outcome.message, err=failed or outcome.status == "unreachable")
        return failed

    @staticmethod
    def deprovision() -> bool:
        """Remove biffd's service; return whether that failed.

        A host with no usable service manager has nothing to remove and prints
        nothing.
        """
        outcome = BiffdService.for_host().uninstall()
        failed = outcome.status == "failed"
        if outcome.status == "done" or failed:
            typer.echo(outcome.message, err=failed)
        return failed
