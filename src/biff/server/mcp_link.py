"""The MCP server's connections to biffd: one per session, opened on first use."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Final, Self, final

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from biff.client.connection import ClientIdentity, ConnectionLostError, DaemonClient
from biff.client.nudge import DaemonNudge
from biff.run_dir import RunDir

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from biff.client.held_reply import HeldReply

__all__ = ["FAILURES", "McpLink", "Reply", "UnreadableReplyError"]

logger = logging.getLogger(__name__)

type Connect = Callable[[ClientIdentity], Awaitable[DaemonClient]]

FAILURES: Final = (ConnectionError, OSError, TimeoutError, ValueError)
"""What reaching biffd fails with, besides a refusal (``DaemonError``).

Every one reads as a sentence the model and the person can act on: not
installed, did not start, no token, a hello biffd cannot take.
"""

_OLDER: Final = (
    "biffd is older than this biff mcp, or sent a reply it cannot read; "
    "run: biff daemon restart"
)


class UnreadableReplyError(ConnectionError):
    """biffd answered with something this server cannot use."""


@final
class Reply(BaseModel):
    """What a tool needs of a command's result: the text, and mail left behind.

    Fields a newer daemon adds are ignored, so the server keeps working across an
    upgrade of biffd; a reply without the text is refused.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    text: str
    more: int = 0

    @classmethod
    def parse(cls, raw: JsonValue) -> Self:
        """Return *raw* as a reply.

        Raises:
            UnreadableReplyError: *raw* has no ``text``, as an older daemon's reply.
        """
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            raise UnreadableReplyError(_OLDER) from exc


@final
class McpLink:
    """Asks biffd on behalf of the sessions one ``biff mcp`` process serves.

    A session's connection is opened at its first request, starting biffd when it
    is down, and kept.  A connection that was lost is opened again once and the
    request repeated: every request that goes through here is a read, so
    repeating it changes nothing.  This process holds no relay connection for
    what it asks here.
    """

    __slots__ = ("_clients", "_connect", "_cwd", "_lock", "_user")

    _user: str
    _cwd: str
    _connect: Connect
    _clients: dict[str, DaemonClient]
    _lock: asyncio.Lock

    def __new__(cls, *, user: str, cwd: str, connect: Connect) -> Self:
        """Link the sessions of *user*, working in *cwd*, through *connect*."""
        self = super().__new__(cls)
        self._user = user
        self._cwd = cwd
        self._connect = connect
        self._clients = {}
        self._lock = asyncio.Lock()
        return self

    @classmethod
    def for_host(cls, *, user: str, cwd: str) -> Self:
        """Return the link that starts biffd on this host when it is down."""
        run_dir = RunDir.default()

        async def connect(identity: ClientIdentity) -> DaemonClient:
            # Blocking: it can wait on the service manager, so it leaves the loop.
            port = await asyncio.to_thread(DaemonNudge.for_host(run_dir).ensure_running)
            return await DaemonClient.connect(
                run_dir=run_dir, port=port, identity=identity
            )

        return cls(user=user, cwd=cwd, connect=connect)

    async def ask(self, session_id: str, method: str, **params: JsonValue) -> Reply:
        """Run *method* for *session_id* and return its reply.

        Raises:
            ConnectionError: biffd is absent, cannot start, or sent a reply this
                server cannot read.
            DaemonError: biffd refused the request.
        """
        held = await self.ask_held(session_id, method, **params)
        await held.confirm()
        return Reply.parse(held.result)

    async def ask_held(
        self, session_id: str, method: str, **params: JsonValue
    ) -> HeldReply:
        """Run *method* for *session_id*; the caller confirms the reply when done.

        Mail a ``read`` carries stays unread until :meth:`HeldReply.confirm`.

        Raises:
            ConnectionError: biffd is absent or cannot start.
            DaemonError: biffd refused the request.
        """
        try:
            return await (await self._client(session_id)).request_held(method, **params)
        except ConnectionLostError:
            logger.info("connection to biffd lost; opening it again")
            self._clients.pop(session_id, None)
            return await (await self._client(session_id)).request_held(method, **params)

    async def close(self) -> None:
        """Close every connection; biffd detaches the sessions and keeps them."""
        clients, self._clients = list(self._clients.values()), {}
        await asyncio.gather(*(c.close() for c in clients), return_exceptions=True)

    async def _client(self, session_id: str) -> DaemonClient:
        async with self._lock:
            held = self._clients.get(session_id)
            if held is None:
                identity = ClientIdentity(
                    client="mcp",
                    session_id=session_id,
                    cwd=self._cwd,
                    user=self._user,
                )
                held = await self._connect(identity)
                self._clients[session_id] = held
            return held
