"""Run biffd in the foreground and turn a startup failure into one line."""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import sys
from importlib import import_module
from importlib.metadata import version as pkg_version
from typing import TYPE_CHECKING, Self, cast, final

import typer
from punt_observe.logging import Role

from biff.daemon.environment import DaemonEnvironment
from biff.daemon.handshake_log import HandshakeLog
from biff.daemon.session_store import CliSessionStore
from biff.logging_config import configure_logging
from biff.run_dir import RunDir
from biff.supervision.biffd import PERMANENT_EXIT_STATUS

if TYPE_CHECKING:
    from biff.daemon.server import BiffDaemon

__all__ = ["Foreground"]

logger = logging.getLogger(__name__)


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
                directory or token file is not private.  One line says what to do,
                on stderr and in the log, and the exit status is the one a unit is
                told not to restart.
        """
        # Only CRITICAL goes to stderr: under a LaunchAgent stderr is biff.log, and
        # every WARNING the file handler wrote was written a second time.
        configure_logging(Role.DAEMON, stderr_level="CRITICAL")
        HandshakeLog.install()
        run_dir = RunDir.default()
        # The first line biffd writes: under a service manager nothing else says
        # that it was started, or by which interpreter, before it listens.
        logger.info(
            "biffd starting: version=%s pid=%d port=%d run_dir=%s python=%s",
            pkg_version("punt-biff"),
            os.getpid(),
            self._port,
            run_dir.path,
            sys.executable,
        )
        removed = DaemonEnvironment.scrub(os.environ)
        if removed:
            typer.echo(
                f"biffd: ignoring {', '.join(removed)} from its environment; "
                "each session resolves its own",
                err=True,
            )
        # Loaded here, after the line above: the relay stack is most of biffd's
        # start-up time, and a daemon that is still importing it has said so.
        server = import_module("biff.daemon.server")
        daemon = cast(
            "BiffDaemon",
            server.BiffDaemon(
                run_dir=run_dir, port=self._port, sessions=CliSessionStore.default()
            ),
        )
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
        """Report *message* as biffd's one-line error and exit permanently.

        It goes to the log as well as stderr: under a service manager nobody reads
        stderr, and the log is where the failure message sends the user.
        """
        logger.error("%s", message)
        typer.echo(f"biffd: {message}", err=True)
        raise typer.Exit(code=PERMANENT_EXIT_STATUS) from None
