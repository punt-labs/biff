"""The ``biff daemon serve`` command."""

from __future__ import annotations

from typing import Annotated, final

import typer

from biff.daemon.foreground import Foreground
from biff.run_dir import DEFAULT_PORT

__all__ = ["DaemonCommands"]


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

        Exits 78 with one line saying what to do when the daemon cannot start:
        the port is taken, or the run directory or token file is not private.
        """
        Foreground(port).run()
