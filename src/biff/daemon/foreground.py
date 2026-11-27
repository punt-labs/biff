"""Run biffd in the foreground and turn a startup failure into one line."""

from __future__ import annotations

import asyncio
import errno
import os
from typing import Self, final

import typer
from punt_observe.logging import Role

from biff.daemon.environment import DaemonEnvironment
from biff.daemon.server import BiffDaemon
from biff.logging_config import configure_logging
from biff.run_dir import RunDir

__all__ = ["Foreground"]


@final
class Foreground:
    """One foreground run of the daemon on a loopback port."""

    __slots__ = ("_port",)

    _port: int

    def __new__(cls, port: int) -> Self:
        """Prepare a run on *port*; 0 picks a free one."""
        self = super().__new__(cls)
        self._port = port
        return self

    def run(self) -> None:
        """Run the daemon until interrupted.

        Raises:
            typer.Exit: The daemon cannot start: the port is taken, or the run
                directory or token file is not private.  One line says what to do.
        """
        configure_logging(Role.DAEMON)
        removed = DaemonEnvironment.scrub(os.environ)
        if removed:
            typer.echo(
                f"biffd: ignoring {', '.join(removed)} from its environment; "
                "each session resolves its own",
                err=True,
            )
        daemon = BiffDaemon(run_dir=RunDir.default(), port=self._port)
        try:
            asyncio.run(daemon.run_foreground())
        except PermissionError as exc:
            self._fail(str(exc))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                self._fail(
                    f"port {self._port} is already in use; see what holds it with: "
                    f"lsof -nP -iTCP:{self._port} -sTCP:LISTEN"
                )
            self._fail(f"cannot listen on 127.0.0.1:{self._port}: {exc}")
        except ValueError as exc:
            self._fail(str(exc))

    @staticmethod
    def _fail(message: str) -> None:
        """Print *message* as biffd's one-line error and exit 1."""
        typer.echo(f"biffd: {message}", err=True)
        raise typer.Exit(code=1) from None
