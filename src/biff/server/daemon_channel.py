"""Feed the channel emitter from biffd's subscription instead of a relay of its own."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Final, Self, cast, final

from biff.server.channel_emitter import ChannelEmitter
from biff.server.daemon_arrivals import ArrivalsSnapshot, DaemonArrivals, DaemonGate

if TYPE_CHECKING:
    from biff.client.connection import DaemonClient
    from biff.frames import Event
    from biff.server.channel_check import ChannelCheck
    from biff.server.channel_rate import RateLimit
    from biff.server.mcp_link import McpLink
    from biff.session_routing import SessionRouting

__all__ = ["DaemonChannelFeed"]

logger = logging.getLogger(__name__)

RETRY_SECONDS: Final = 1.0
"""How long after a lost subscription the feed opens it again, at first."""

MAX_RETRY_SECONDS: Final = 10.0

STABLE_SECONDS: Final = 30.0
"""How long a subscription must stay up before its loss starts the backoff afresh."""


@final
class DaemonChannelFeed:
    """Subscribes to biffd for one session and drives the channel emitter from it.

    It holds a connection of its own, which attaches to the session beside the
    one the tools use.  Each event of biffd (an arrival summary, never a body) is
    followed by one ``arrivals`` request, so what the emitter reads is what is
    true now, and by a pass of the emitter, which keeps its rate limit, held
    kinds and retry.  A lost connection is opened again, and biffd's summary on
    the new subscription announces the mail that came meanwhile.  A new session
    id (``/clear``, a resume) moves the feed to that session.
    """

    __slots__ = (
        "_arrivals",
        "_emitter",
        "_link",
        "_pass",
        "_retry",
        "_stable",
        "_task",
        "_up_since",
        "_wait",
    )

    _link: McpLink
    _arrivals: DaemonArrivals
    _emitter: ChannelEmitter
    _retry: float
    _pass: asyncio.Event
    _task: asyncio.Task[None] | None
    _wait: float
    _stable: float
    _up_since: float | None

    def __new__(
        cls,
        *,
        link: McpLink,
        check: ChannelCheck,
        retry_seconds: float = RETRY_SECONDS,
        limit: RateLimit | None = None,
        stable_seconds: float = STABLE_SECONDS,
    ) -> Self:
        """Feed an emitter, made here, from the subscriptions *link* opens.

        *check* is the session's channel check, which the emitter owes tokens to;
        *limit* is the emitter's rate limit, the design's by default.
        """
        self = super().__new__(cls)
        self._link = link
        self._arrivals = DaemonArrivals()
        self._emitter = ChannelEmitter(
            None,
            self.poke,
            check=check,
            limit=limit,
            arrivals=self._arrivals,
            gate=DaemonGate(self._arrivals),
        )
        self._retry = retry_seconds
        self._wait = retry_seconds
        self._stable = stable_seconds
        self._up_since = None
        self._pass = asyncio.Event()
        self._task = None
        return self

    @property
    def emitter(self) -> ChannelEmitter:
        """The emitter this feed drives; the server sends its events."""
        return self._emitter

    def poke(self) -> None:
        """Ask for a pass of the emitter: a gate opened, or a tool was called."""
        self._pass.set()

    def bind(self, routing: SessionRouting) -> None:
        """Follow the session *routing* names, leaving the one it followed.

        What was sent, seen and known of the old session is dropped once nothing
        of it is in flight: an inbox's mark orders mail within that inbox only, so
        a new session may start lower, and a send still running for the old one
        must not record its mark against the new one.
        """
        previous = self._task
        self._task = asyncio.create_task(
            self._move(previous, routing.session_id), name="channel feed"
        )
        self._task.add_done_callback(self._ended)

    async def _move(self, previous: asyncio.Task[None] | None, session_id: str) -> None:
        """Stop the old session's feed and sends, forget it, then follow the new."""
        if previous is not None:
            previous.cancel()
            await asyncio.gather(previous, return_exceptions=True)
        await self._emitter.reset()
        self._arrivals.forget()
        await self._follow(session_id)

    @staticmethod
    def _ended(task: asyncio.Task[None]) -> None:
        """Say what ended a feed task nobody cancelled: nothing else is watching."""
        if not task.cancelled() and (exc := task.exception()) is not None:
            logger.error("the channel feed ended with %r", exc, exc_info=exc)

    async def close(self) -> None:
        """Stop following; the connection closes with the task.

        A feed that already died has its error logged, not raised here; a
        cancellation aimed at the caller is the caller's own and propagates.
        """
        task, self._task = self._task, None
        if task is None:
            return
        if task.done():
            if not task.cancelled() and (exc := task.exception()) is not None:
                logger.warning("the channel feed had ended with %s", exc, exc_info=exc)
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _follow(self, session_id: str) -> None:
        """Hold a subscription for *session_id*, opening it again when it is lost.

        Whatever ends a subscription, an unexpected error included, is logged and
        followed by another try: a feed that stopped quietly would leave the
        channel dead until the next ``/clear``.
        """
        self._wait = self._retry
        while True:
            try:
                await self._subscribed(session_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._backoff_after_loss()
                logger.warning(
                    "channel events from biffd stopped: %s; opening again in %.1fs",
                    exc,
                    self._wait,
                    exc_info=True,
                )
                await asyncio.sleep(self._wait)
                self._wait = min(self._wait * 2, MAX_RETRY_SECONDS)

    def _backoff_after_loss(self) -> None:
        """Start the wait afresh if the lost subscription had stayed up long enough.

        One that dies at once (biffd crash-looping, or a pass that raises every
        time) must back off, not retry at the base delay for ever.
        """
        up, self._up_since = self._up_since, None
        if up is not None and asyncio.get_running_loop().time() - up >= self._stable:
            self._wait = self._retry

    async def _subscribed(self, session_id: str) -> None:
        """Run one subscription: summary, then each arrival, until it is lost."""
        client = await self._link.open(session_id)
        try:
            who = cast("dict[str, object]", await self._data(client, "status"))
            user, _, tty = str(who["session_key"]).partition(":")
            self._arrivals.is_whom(user, frozenset({tty, str(who["tty_name"])}))
            await client.subscribe()
            await self._refresh(client)
            self._arrivals.subscribed()  # what waited before this subscription
            self._emitter.reconnected()  # what came while no subscription ran
            self._up_since = asyncio.get_running_loop().time()
            try:
                async with asyncio.TaskGroup() as group:
                    group.create_task(self._passes(client))
                    group.create_task(self._events(client))
            except ExceptionGroup as failed:
                # One task failed and the group cancelled the other: report the
                # cause itself, so the caller sees a connection error, not a group.
                raise failed.exceptions[0] from failed
        finally:
            await client.close()

    async def _events(self, client: DaemonClient) -> None:
        while True:
            event = await client.next_event()
            self._take(event)
            await self._refresh(client)
            self._pass.set()

    def _take(self, event: Event) -> None:
        if event.kind == "channel_check_request":
            data = event.data
            token = data.get("token") if isinstance(data, dict) else None
            if isinstance(token, str) and token:
                self._emitter.owe_check(token)
            else:
                logger.warning(
                    "dropped a channel check request with no token (data: %r)", data
                )
        elif event.kind == "recovered":
            self._emitter.reconnected()
        else:
            self._arrivals.count(event)

    async def _refresh(self, client: DaemonClient) -> None:
        data = cast("dict[str, object]", await self._data(client, "arrivals"))
        self._arrivals.refresh(ArrivalsSnapshot.of(data))

    @staticmethod
    async def _data(client: DaemonClient, method: str) -> object:
        reply = await client.request(method)
        if not isinstance(reply, dict) or not isinstance(reply.get("data"), dict):
            msg = f"biffd answered {method} without its data"
            raise ConnectionError(msg)
        return reply["data"]

    async def _passes(self, client: DaemonClient) -> None:
        """Run the emitter once per poke; pokes that arrive together are one pass.

        Each pass starts from a fresh snapshot, as the in-process emitter reads
        the relay each time: a ``mesg`` turned back on, or a read, changes what is
        owed without any event.
        """
        while True:
            await self._pass.wait()
            self._pass.clear()
            await self._refresh(client)
            await self._emitter.check()
