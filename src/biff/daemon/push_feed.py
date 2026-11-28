"""A session's push subscriptions: notices that mail or a wall arrived."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Protocol, Self, cast, final, runtime_checkable

from biff.channel_check_pending import CheckRefusedError
from biff.channel_check_wire import CheckRequest
from biff.daemon.check_hop import CheckHop, ForeignReplyError
from biff.daemon.tasks import Supervised
from biff.daemon.wakes import Wakes
from biff.relay_errors import RELAY_ERRORS
from biff.talk_latch import TalkNotifyLatch
from biff.talk_types import TalkNotification

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from biff.cli_context import CliContext
    from biff.daemon.talk_feed import TalkClient, TalkMessage, TalkSubscription
    from biff.relay import Relay

__all__ = ["PushFeed", "PushTransport"]

logger = logging.getLogger(__name__)


@runtime_checkable
class PushTransport(Protocol):
    """A relay that pushes arrival notices: only the NATS relay does."""

    @property
    def connection_generation(self) -> int:
        """A token that rises each time the relay dials a new client."""
        ...

    def talk_notify_subject(self, session_key: str) -> str:
        """Return the subject a targeted message's wake poke rides."""
        ...

    def channel_check_subject(self, session_key: str) -> str:
        """Return the subject a doctor's channel check request rides."""
        ...

    def inbox_notify_subject(self, repo: str, user: str) -> str:
        """Return the subject a broadcast message's wake poke rides."""
        ...

    def wall_notify_subject(self, repo: str) -> str:
        """Return the subject a wall change's wake poke rides."""
        ...

    async def publish_channel_check_refusal(self, reply: str, reason: str) -> None:
        """Tell the doctor listening on *reply* why its request was refused."""
        ...

    async def get_nc(self) -> TalkClient:
        """Return the live NATS client."""
        ...


@final
class PushFeed:
    """Turns the relay's pokes for a session into wakes of its poll.

    The relay publishes a poke when mail or a wall arrives: a payload-less frame
    to the repository's subject for the user (broadcast mail) or the repository
    (a wall), and a frame without a talk type to the session's talk subject
    (mail addressed to it).  The feed pokes the session's :class:`Wakes` for each,
    so the poll reads the arrival at once instead of at its next interval.  It
    announces nothing itself: the poll decides what is new, so a poke that is
    duplicated or lost changes nothing.  As the talk feed does, it subscribes
    again when the relay dials a new client.
    """

    __slots__ = (
        "_checks",
        "_generation",
        "_handles",
        "_interval",
        "_latch",
        "_relay",
        "_repo",
        "_session_key",
        "_task",
        "_user",
        "_wakes",
    )

    _relay: Relay
    _session_key: str
    _user: str
    _repo: str
    _wakes: Wakes
    _checks: CheckHop
    _interval: float
    _handles: list[TalkSubscription]
    _generation: int
    _latch: TalkNotifyLatch
    _task: asyncio.Task[None] | None

    def __new__(
        cls,
        *,
        relay: Relay,
        session_key: str,
        user: str,
        repo: str,
        interval: float,
    ) -> Self:
        """Create a feed for *session_key*, checked every *interval* seconds."""
        self = super().__new__(cls)
        self._relay = relay
        self._session_key = session_key
        self._user = user
        self._repo = repo
        self._wakes = Wakes()
        self._checks = CheckHop()
        self._interval = interval
        self._handles = []
        self._generation = 0
        self._latch = TalkNotifyLatch.for_resubscribe(logger)
        self._task = None
        return self

    @classmethod
    def of(cls, ctx: CliContext, interval: float) -> Self:
        """Create the feed of the session *ctx* describes."""
        return cls(
            relay=ctx.relay,
            session_key=ctx.session_key,
            user=ctx.user,
            repo=ctx.config.repo_name,
            interval=interval,
        )

    @property
    def wakes(self) -> Wakes:
        """The wake flags this feed sets."""
        return self._wakes

    @property
    def checks(self) -> CheckHop:
        """The channel check this feed's requests are made into."""
        return self._checks

    def start(self) -> None:
        """Begin feeding, unless running or the relay pushes nothing."""
        if not isinstance(self._relay, PushTransport):
            return
        if self._task is None or self._task.done():
            self._task = Supervised.spawn(self._run(), f"push {self._session_key}")

    def stop(self) -> None:
        """Stop feeding and release the subscriptions."""
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        try:
            while True:
                try:
                    await self._reconcile()
                except Exception:
                    logger.exception("push subscription check failed")
                await asyncio.sleep(self._interval)
        finally:
            await self._unsubscribe()

    async def _reconcile(self) -> None:
        relay = self._relay
        if not isinstance(relay, PushTransport):
            return
        if self._handles and self._generation >= relay.connection_generation:
            return
        await self._unsubscribe()
        try:
            nc = await relay.get_nc()
            generation = relay.connection_generation
            taps: tuple[tuple[str, Callable[[TalkMessage], Awaitable[None]]], ...] = (
                (relay.talk_notify_subject(self._session_key), self._on_talk),
                (relay.channel_check_subject(self._session_key), self._on_check),
                (relay.inbox_notify_subject(self._repo, self._user), self._on_poke),
                (relay.wall_notify_subject(self._repo), self._on_poke),
            )
            for subject, handler in taps:
                self._handles.append(await nc.subscribe(subject, cb=handler))
            self._generation = generation
            await nc.flush()
        except RELAY_ERRORS:
            await self._unsubscribe()
            self._latch.record_failure()
            return
        self._latch.record_success()

    async def _unsubscribe(self) -> None:
        handles, self._handles = self._handles, []
        for handle in handles:
            try:
                await handle.unsubscribe()
            except RELAY_ERRORS:
                logger.debug("push unsubscribe failed", exc_info=True)

    async def _on_check(self, msg: TalkMessage) -> None:
        """Turn a doctor's request into a token, or drop what is not a request."""
        try:
            request = CheckRequest.from_payload(msg.data)
        except ValueError:
            logger.debug("channel check request dropped: malformed", exc_info=True)
            return
        try:
            self._checks.request(request.reply)
        except ForeignReplyError as exc:
            logger.warning("channel check refused for %s: %s", self._session_key, exc)
        except CheckRefusedError as exc:
            logger.warning("channel check refused for %s: %s", self._session_key, exc)
            await self._refuse(request.reply, str(exc))

    async def _refuse(self, reply: str, reason: str) -> None:
        """Tell the doctor why, so it does not wait out the check."""
        relay = self._relay
        if not isinstance(relay, PushTransport):
            return
        try:
            await relay.publish_channel_check_refusal(reply, reason)
        except RELAY_ERRORS:
            logger.warning("channel check refusal not sent", exc_info=True)

    async def _on_poke(self, _msg: TalkMessage) -> None:
        self._wakes.poke()

    async def _on_talk(self, msg: TalkMessage) -> None:
        """Poke for a frame without a talk type: mail addressed to the session."""
        try:
            raw: object = json.loads(msg.data)
        except ValueError:
            self._wakes.poke()  # a bare wake, as the relay once sent
            return
        if not isinstance(raw, dict):
            self._wakes.poke()
            return
        pairs = cast("dict[object, object]", raw).items()
        frame: dict[str, object] = {str(k): v for k, v in pairs}
        if TalkNotification.from_payload(frame).is_wake_poke:
            self._wakes.poke()
