"""The MCP server's connection to biffd: one session at a time, opened on first use."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Self, final

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from biff.client.connection import (
    ClientIdentity,
    ConnectionLostError,
    DaemonClient,
    DaemonError,
)
from biff.client.nudge import DaemonNudge
from biff.run_dir import RunDir

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from biff.client.held_reply import HeldReply

__all__ = [
    "FAILURES",
    "McpLink",
    "Reply",
    "UnreadableReplyError",
]

logger = logging.getLogger(__name__)

type Connect = Callable[[ClientIdentity], Awaitable[DaemonClient]]
type Action = Callable[[], Awaitable[None]]

FAILURES: Final = (ConnectionError, OSError, TimeoutError, ValueError)
"""What reaching biffd fails with, besides a refusal (``DaemonError``).

Every one reads as a sentence the model and the person can act on: not
installed, did not start, no token, a hello biffd cannot take.
"""

SETTLE_SECONDS: Final = 5.0
"""How long a process must outlive a reply before its mail counts as delivered."""

_REPEATABLE: Final = frozenset({"who", "finger", "last", "status", "read", "talk_read"})
"""The requests that are safe to send again after a connection was lost.

Any other may already have been carried out: a repeated ``write`` is a second
message, a repeated ``wall`` a second post.
"""

_OLDER: Final = (
    "biffd is older than this biff mcp, or sent a reply it cannot read; "
    "run: biff daemon restart"
)


class UnreadableReplyError(ConnectionError):
    """biffd answered with something this server cannot use."""


class UncertainRequestError(ConnectionError):
    """The connection was lost with a request in flight that is not safe to repeat."""


@final
class Reply(BaseModel):
    """What a tool needs of a command's result: the text, and mail left behind.

    Fields a newer daemon adds are ignored, so the server keeps working across an
    upgrade of biffd; a reply without the text is refused.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    text: str
    data: JsonValue = None
    error: bool = False
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

    @property
    def carries_mail(self) -> bool:
        """Whether a ``read`` reply holds at least one message."""
        return isinstance(self.data, list) and bool(self.data)


@final
@dataclass(frozen=True)
class _Pending:
    """A ``read`` whose mail biffd still counts unread, and what follows its release."""

    held: HeldReply
    then: Action
    timer: asyncio.Task[None]


@final
class McpLink:
    """Asks biffd on behalf of the one session a ``biff mcp`` process serves.

    The connection is opened at the first request, starting biffd when it is
    down, and kept.  One that was lost is opened again, and a request that is
    safe to repeat (a look, or a ``read``, which only holds its mail until it is
    confirmed) is sent again; any other fails, because biffd may have carried it
    out.  A request for another session (``/clear`` or a resume gave the
    process a new one) settles and closes the old connection first.  This
    process holds no relay connection for what it asks here.

    **Mail is confirmed late.**  The tool's text reaches the client some time
    after the tool returns, and nothing here can see that happen, so biffd is
    told the mail was delivered only when the process has outlived the reply by
    ``settle_seconds`` (a failed send ends the process well before that) or the
    session makes its next request.  A process that dies in between leaves the
    mail unread, and the next read serves it again.
    """

    __slots__ = (
        "_clients",
        "_connect",
        "_cwd",
        "_lock",
        "_pending",
        "_settle_seconds",
        "_user",
    )

    _user: str
    _cwd: str
    _connect: Connect
    _settle_seconds: float
    _clients: dict[str, DaemonClient]
    _pending: dict[str, _Pending]
    _lock: asyncio.Lock

    def __new__(
        cls,
        *,
        user: str,
        cwd: str,
        connect: Connect,
        settle_seconds: float = SETTLE_SECONDS,
    ) -> Self:
        """Link the sessions of *user*, working in *cwd*, through *connect*."""
        self = super().__new__(cls)
        self._user = user
        self._cwd = cwd
        self._connect = connect
        self._settle_seconds = settle_seconds
        self._clients = {}
        self._pending = {}
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
        held = await self._request(session_id, method, params)
        await held.confirm()
        return Reply.parse(held.result)

    async def read(self, session_id: str, then: Action) -> Reply:
        """Read *session_id*'s mail; it counts delivered later (see the class).

        *then* runs once the mail has been confirmed.

        Raises:
            ConnectionError: biffd is absent, cannot start, or sent a reply this
                server cannot read.
            DaemonError: biffd refused the request.
        """
        held = await self._request(session_id, "read", {})
        reply = Reply.parse(held.result)
        timer = asyncio.create_task(self._settle_later(session_id))
        self._pending[session_id] = _Pending(held, then, timer)
        return reply

    async def close(self) -> None:
        """Close every connection, leaving unconfirmed mail unread.

        biffd detaches the sessions and keeps them.
        """
        for pending in self._pending.values():
            pending.timer.cancel()
        self._pending.clear()
        clients, self._clients = list(self._clients.values()), {}
        await asyncio.gather(*(c.close() for c in clients), return_exceptions=True)

    async def _request(
        self, session_id: str, method: str, params: dict[str, JsonValue]
    ) -> HeldReply:
        await self._settle(session_id)
        client = await self._client(session_id)  # a failure here sent nothing
        try:
            return await client.request_held(method, **params)
        except ConnectionLostError as exc:
            self._clients.pop(session_id, None)
            if method not in _REPEATABLE:
                msg = (
                    f"the connection to biffd was lost while {method} was in flight; "
                    "it may have been carried out, so check before sending it again"
                )
                raise UncertainRequestError(msg) from exc
            logger.info("connection to biffd lost; opening it again")
            return await (await self._client(session_id)).request_held(method, **params)

    async def _client(self, session_id: str) -> DaemonClient:
        async with self._lock:
            for other in [sid for sid in self._clients if sid != session_id]:
                await self._settle(other)
                await self._clients.pop(other).close()
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

    async def _settle_later(self, session_id: str) -> None:
        await asyncio.sleep(self._settle_seconds)
        await self._settle(session_id)

    async def _settle(self, session_id: str) -> None:
        """Confirm the mail *session_id* last read, then run what follows."""
        pending = self._pending.pop(session_id, None)
        if pending is None:
            return
        if pending.timer is not asyncio.current_task():
            pending.timer.cancel()
        try:
            await pending.held.confirm()
            await pending.then()
        except (*FAILURES, DaemonError) as exc:
            logger.warning("mail read is not confirmed to biffd yet: %s", exc)
