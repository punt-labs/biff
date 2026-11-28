"""What the session hooks share when they talk to biffd: connector, files, deadline."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Final, Self, final

from biff._stdlib import biff_data_dir
from biff.client.connection import ClientIdentity
from biff.client.host import HostConnector
from biff.session_handle import SessionHandleStore
from biff.session_routing import SessionIdError, SessionRouting

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from biff.client.connection import DaemonClient

__all__ = ["DEADLINE_SECONDS", "HookDaemon"]

DEADLINE_SECONDS: Final = 8.0
"""How long a session hook waits for biffd; SessionEnd's script allows it 10 s."""


@final
class HookDaemon:
    """The way one session hook reaches biffd and where it keeps its files."""

    __slots__ = ("_connect", "_deadline", "_handles", "_unread_dir")

    _connect: Callable[[ClientIdentity], Awaitable[DaemonClient]]
    _unread_dir: Path
    _handles: SessionHandleStore
    _deadline: float

    def __new__(
        cls,
        *,
        connect: Callable[[ClientIdentity], Awaitable[DaemonClient]],
        unread_dir: Path,
        handles: SessionHandleStore,
        deadline: float,
    ) -> Self:
        """Connect with *connect*, keep files in these places, wait *deadline* s."""
        self = super().__new__(cls)
        self._connect = connect
        self._unread_dir = unread_dir
        self._handles = handles
        self._deadline = deadline
        return self

    @classmethod
    def for_host(cls) -> Self:
        """Return the hook's access to this host's biffd, started on demand."""
        data_root = biff_data_dir()
        return cls(
            connect=HostConnector.for_host().connect,
            unread_dir=data_root / "unread",
            handles=SessionHandleStore(data_root / "sessions"),
            deadline=DEADLINE_SECONDS,
        )

    @property
    def unread_dir(self) -> Path:
        """Where the per-session unread status files live."""
        return self._unread_dir

    @property
    def handles(self) -> SessionHandleStore:
        """The per-session handle files."""
        return self._handles

    def deadline(self) -> asyncio.Timeout:
        """Return a context that raises ``TimeoutError`` when the wait is over."""
        return asyncio.timeout(self._deadline)

    async def client(
        self,
        routing: SessionRouting,
        cwd: str,
        *,
        ends: bool = False,
        run: str = "",
    ) -> DaemonClient:
        """Say hello to biffd as a hook of *routing*'s session, working in *cwd*."""
        identity = ClientIdentity(
            client="hook",
            session_id=routing.session_id,
            cwd=cwd,
            main_agent_id=run,
            ends=ends,
        )
        return await self._connect(identity)

    @staticmethod
    def routing_of(data: dict[str, object]) -> SessionRouting | None:
        """Return the session a hook payload names, or ``None`` when it names none.

        A missing or malformed ``session_id`` is the harness failing to declare
        itself, and a hook never breaks over that.
        """
        raw = data.get("session_id")
        try:
            return SessionRouting(raw if isinstance(raw, str) else "")
        except SessionIdError:
            return None

    @staticmethod
    def cwd_of(data: dict[str, object]) -> str:
        """Return the directory the payload says the harness runs in."""
        cwd = data.get("cwd")
        return cwd if isinstance(cwd, str) and cwd else str(Path.cwd())

    @staticmethod
    def run_of(environ: Mapping[str, str]) -> str:
        """Return the harness process id as the run this hook belongs to, or "".

        It is only ever used to refuse an end that is not for the live run, so a
        recycled pid costs nothing worse than a session left to expire.
        """
        pid = environ.get("CLAUDE_PID", "")
        return pid if pid.isdecimal() else ""
