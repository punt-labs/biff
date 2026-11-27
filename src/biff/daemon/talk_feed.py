"""A session's talk subscription: frames from the relay into its talk state."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Protocol, Self, cast, final, runtime_checkable

from biff.daemon.talk_notices import TalkNotice
from biff.daemon.tasks import Supervised
from biff.relay_errors import RELAY_ERRORS
from biff.talk_latch import TalkNotifyLatch
from biff.talk_types import TalkNotification

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from biff.daemon.talk_notices import TalkNotices
    from biff.relay import Relay
    from biff.talk_state import TalkState

__all__ = [
    "TalkClient",
    "TalkFeed",
    "TalkMessage",
    "TalkSubscription",
    "TalkTransport",
]

logger = logging.getLogger(__name__)


class TalkMessage(Protocol):
    """A message a subscription delivers."""

    @property
    def data(self) -> bytes:
        """The payload."""
        ...


class TalkSubscription(Protocol):
    """A live subscription to a talk subject."""

    async def unsubscribe(self) -> None:
        """Stop receiving."""
        ...


class TalkClient(Protocol):
    """The part of a NATS client the talk feed uses."""

    async def subscribe(
        self, subject: str, *, cb: Callable[[TalkMessage], Awaitable[None]]
    ) -> TalkSubscription:
        """Call *cb* for each message on *subject*."""
        ...

    async def flush(self) -> None:
        """Return once the server has processed everything sent so far."""
        ...


@runtime_checkable
class TalkTransport(Protocol):
    """A relay that carries talk frames: only the NATS relay does."""

    @property
    def connection_generation(self) -> int:
        """A token that rises each time the relay dials a new client."""
        ...

    def talk_notify_subject(self, session_key: str) -> str:
        """Return the subject talk frames for *session_key* ride."""
        ...

    async def get_nc(self) -> TalkClient:
        """Return the live NATS client."""
        ...


@final
class TalkFeed:
    """Feeds one session's talk frames into its :class:`TalkState`.

    The daemon holds the subscription, so a conversation survives a client that
    comes and goes.  A wedge teardown replaces the relay's NATS client and
    orphans the subscription on the dead one; the feed notices the rise in
    ``connection_generation`` and subscribes again, while an in-place nats-py
    reconnect, which replays every subscription, leaves it alone
    (nats-relay.tex ``talkSubGen``).  Each real frame becomes a notice.
    """

    __slots__ = (
        "_generation",
        "_handle",
        "_interval",
        "_latch",
        "_notices",
        "_relay",
        "_session_key",
        "_subscribed",
        "_talk",
        "_task",
    )

    _relay: Relay
    _session_key: str
    _talk: TalkState
    _notices: TalkNotices
    _interval: float
    _handle: TalkSubscription | None
    _generation: int
    _latch: TalkNotifyLatch
    _task: asyncio.Task[None] | None
    _subscribed: asyncio.Event

    def __new__(
        cls,
        *,
        relay: Relay,
        session_key: str,
        talk: TalkState,
        notices: TalkNotices,
        interval: float,
    ) -> Self:
        """Create a feed that checks its subscription every *interval* seconds."""
        self = super().__new__(cls)
        self._relay = relay
        self._session_key = session_key
        self._talk = talk
        self._notices = notices
        self._interval = interval
        self._handle = None
        self._generation = 0
        self._latch = TalkNotifyLatch.for_resubscribe(logger)
        self._task = None
        self._subscribed = asyncio.Event()
        return self

    def start(self) -> None:
        """Begin feeding, unless running or the relay carries no talk."""
        if not isinstance(self._relay, TalkTransport):
            return
        if self._task is None or self._task.done():
            self._task = Supervised.spawn(self._run(), f"talk {self._session_key}")

    async def ready(self) -> None:
        """Return once the subscription exists, or at once if there is no talk.

        Frames are ephemeral: one that arrives before the subscription does is
        lost, so a client is told it is attached only after this returns.
        """
        if isinstance(self._relay, TalkTransport) and self._task is not None:
            await self._subscribed.wait()

    def stop(self) -> None:
        """Stop feeding and release the subscription."""
        self._confirmed(yes=False)
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        try:
            while True:
                try:
                    await self._reconcile()
                except Exception:
                    # A bug here must not end talk for the session, nor go
                    # unlogged: report it and try again on the next tick.
                    logger.exception("talk subscription check failed")
                await asyncio.sleep(self._interval)
        finally:
            await self._unsubscribe()

    async def _reconcile(self) -> None:
        """Subscribe, or subscribe again when the relay dialled a new client."""
        relay = self._relay
        if not isinstance(relay, TalkTransport):
            return
        if self._handle is not None and self._generation >= relay.connection_generation:
            return
        await self._unsubscribe()
        try:
            nc = await relay.get_nc()
            generation = relay.connection_generation
            subject = relay.talk_notify_subject(self._session_key)
            # Held from the moment it exists, so a failure or a cancellation in the
            # confirmation below releases it: a leaked subscription would queue
            # every later frame twice.
            self._handle = await nc.subscribe(subject, cb=self._on_frame)
            self._generation = generation
            # The client only queues the subscription; the server acts on it once
            # this round trip returns, and a frame sent before that is lost.
            await nc.flush()
        except RELAY_ERRORS:
            # A dial in progress can make either call raise; the next tick retries.
            await self._unsubscribe()
            self._latch.record_failure()
            return
        self._confirmed(yes=True)
        self._latch.record_success()

    def _confirmed(self, *, yes: bool) -> None:
        """Record whether the server holds the subscription; talk reads report it."""
        if yes:
            self._subscribed.set()
        else:
            self._subscribed.clear()
        self._talk.set_subscribed(confirmed=yes)

    async def _unsubscribe(self) -> None:
        self._confirmed(yes=False)
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            await handle.unsubscribe()
        except RELAY_ERRORS:
            # A superseding dial already closed the client; there is nothing left
            # to release.
            logger.debug("talk unsubscribe failed", exc_info=True)

    async def _on_frame(self, msg: TalkMessage) -> None:
        """Queue one frame, and tell listeners what it was."""
        try:
            raw: object = json.loads(msg.data)
        except ValueError:
            logger.debug("dropping a talk frame that is not JSON")
            return
        if not isinstance(raw, dict):
            return
        frame = cast("dict[str, object]", raw)  # JSON object keys are strings
        if not self._talk.receive(frame):
            return
        notif = TalkNotification.from_payload(frame)
        if TalkNotice.is_noticed(notif):
            self._notices.publish(TalkNotice.of(notif, pending=self._talk.queued))
