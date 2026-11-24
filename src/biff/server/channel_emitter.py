"""Send Claude Code channel events when mail, walls and talk arrive (DES-087).

The emitter is driven by arrivals: the poller calls :meth:`ChannelEmitter.check`
on a pass that a relay push, a wake poke, a tool call or a deadline caused.  It
never runs on a cadence.  The one timer it may arm is a one-shot that sends an
event the rate limit held back, armed only while one is held.

``docs/notification.tex`` (Channel Delivery) models it: ``chanNewer`` is "the
current state is newer than the last-sent record", ``chanHeld`` is "held by the
rate limit", ``chanRetried`` and ``chanDormant`` are the two-attempt cycle after
a timed-out send.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, Self, runtime_checkable

from biff.failure_log import EpisodeLog
from biff.relay_errors import RELAY_ERRORS
from biff.server.channel import ChannelLink, ChannelUnavailableError
from biff.server.channel_content import ChannelEvent, EventKind, Sender
from biff.server.channel_rate import RateLimit, RateWindow

if TYPE_CHECKING:
    from biff.models import WallPost
    from biff.relay import Relay
    from biff.talk_state import TalkState

logger = logging.getLogger(__name__)

RETRY_SECONDS = 1.0
"""How long after a timed-out send the deferred send tries that kind again."""

_EXPECTED = (ChannelUnavailableError, *RELAY_ERRORS)

type WallKey = tuple[datetime, str, str]


class ArrivalSource(Protocol):
    """What the emitter reads: the relay, the talk state and this session's key."""

    @property
    def relay(self) -> Relay:
        """The relay mail and walls arrive through."""
        ...

    @property
    def talk(self) -> TalkState:
        """The held talk state."""
        ...

    @property
    def session_key(self) -> str:
        """This session's ``user:tty`` key."""
        ...


@runtime_checkable
class ReconnectNotifier(Protocol):
    """A relay that reports an in-place reconnect to its listeners."""

    def add_reconnect_listener(self, callback: Callable[[], None]) -> None:
        """Call *callback* each time the connection is back."""
        ...


@dataclass(frozen=True, slots=True)
class _Reading:
    """What one kind looks like right now, taken before any send.

    *fingerprint* changes when something of that kind arrives.  *commit*
    advances the last-sent record to the value this reading took, which is the
    value read before the send, so an arrival during the send survives it.
    """

    kind: EventKind
    fingerprint: Hashable
    is_newer: bool
    event: ChannelEvent | None
    commit: Callable[[], None]

    @property
    def is_owed(self) -> bool:
        """Whether an event is owed: newer than the record, and still true."""
        return self.is_newer and self.event is not None


class ChannelEmitter:
    """Turn arrivals into rate-limited ``notifications/claude/channel`` events.

    With no link (channel mode off) it does nothing and reads nothing.  An event
    goes out only when the client has sent ``notifications/initialized``, mesg is
    on, and the kind's rate window is open; otherwise the kind stays owed with
    its record unchanged and goes out later with current values.
    """

    _source: ArrivalSource
    _wake: Callable[[], None]
    _link: ChannelLink | None
    _rate: RateWindow
    _clock: Callable[[], float]
    _log: EpisodeLog
    _lock: asyncio.Lock
    _is_initialized: bool
    _is_mesg_on: bool
    _mail_sent: int
    _invites_sent: int
    _messages_sent: int
    _walls_sent: set[WallKey]
    _due: dict[EventKind, float]
    _retried: set[EventKind]
    _dormant: dict[EventKind, Hashable]
    _frees_dormant: bool
    _timer: asyncio.TimerHandle | None
    _timer_task: asyncio.Task[None] | None

    def __new__(
        cls,
        source: ArrivalSource,
        wake: Callable[[], None],
        *,
        limit: RateLimit | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> Self:
        """Create a dormant emitter; :meth:`serve` gives it a link.

        Args:
            source: The relay, talk state and session key to read.
            wake: Ask the poller for a pass; called when a gate opens.
            limit: The rate limit; the design's constants by default.
            clock: A monotonic clock, replaced in tests.
        """
        self = super().__new__(cls)
        self._source = source
        self._wake = wake
        self._link = None
        self._clock = clock
        self._rate = RateWindow(limit or RateLimit(), clock)
        self._log = EpisodeLog(logger, _EXPECTED)
        self._lock = asyncio.Lock()
        self._is_initialized = False
        self._is_mesg_on = True
        self._mail_sent = 0
        self._invites_sent = 0
        self._messages_sent = 0
        self._walls_sent = set()
        self._due = {}
        self._retried = set()
        self._dormant = {}
        self._frees_dormant = False
        self._timer = None
        self._timer_task = None
        return self

    # -- Gates ---------------------------------------------------------------

    @property
    def is_enabled(self) -> bool:
        """Whether channel mode is on."""
        return self._link is not None

    def is_timer_armed(self) -> bool:
        """Whether the one-shot deferred send is armed."""
        return self._timer is not None

    @property
    def held(self) -> frozenset[EventKind]:
        """The kinds the rate limit is holding back."""
        return frozenset(self._due)

    def serve(self, link: ChannelLink) -> None:
        """Turn channel mode on for the connection *link* carries.

        Registers for the relay's in-place reconnects, so mail that arrived while
        it was down is announced.  A fresh dial is reported by the poller's
        subscription rebind, which calls :meth:`reconnected` too.
        """
        self._link = link
        relay = self._source.relay
        if isinstance(relay, ReconnectNotifier):
            relay.add_reconnect_listener(self.reconnected)

    def initializing(self) -> None:
        """Close the gate: the client sent ``initialize`` and is not yet ready."""
        self._is_initialized = False

    def initialized(self) -> None:
        """Open the gate: the client sent ``notifications/initialized``."""
        self._is_initialized = True
        if self.is_enabled:
            self._wake()

    def set_mesg(self, *, enabled: bool) -> None:
        """Record ``/mesg y`` or ``/mesg n``; coming on asks for a pass."""
        was_on, self._is_mesg_on = self._is_mesg_on, enabled
        if enabled and not was_on and self.is_enabled:
            self._wake()

    def reconnected(self) -> None:
        """Run the sender once: the relay is back and may have missed an arrival."""
        if self.is_enabled:
            self._frees_dormant = True
            self._wake()

    def tool_called(self) -> None:
        """Let a kind that timed out twice try again on the pass a call wakes."""
        if self._dormant:
            self._frees_dormant = True
            self._wake()

    # -- The sender ----------------------------------------------------------

    async def check(self) -> None:
        """Announce whatever is owed; called on a poller pass an arrival caused."""
        if self._link is None or self._is_gated:
            return
        async with self._lock:
            if self._gate_closed():  # a gate closed while this waited for the lock
                return
            frees, self._frees_dormant = self._frees_dormant, False
            for kind in EventKind:
                reading = await self._read(kind)
                if reading is None or self._is_asleep(reading, frees=frees):
                    continue
                await self._consider(reading)
            self._rearm()

    async def deferred_send(self) -> None:
        """Handle the one-shot timer: send, drop or release each kind that is due.

        For each held kind whose time has come: send it; drop it when nothing is
        owed any more; release it, record unchanged, when mesg is off or the
        client is not initialised; or, when the send times out, hold it for one
        more try.  It never wakes a kind that went dormant.
        """
        async with self._lock:
            self._timer = None
            now = self._clock()
            for kind in [k for k, due in self._due.items() if due <= now]:
                if self._link is None or self._is_gated:
                    self._release(kind)
                    continue
                reading = await self._read(kind)
                if reading is None:
                    self._release(kind)
                else:
                    await self._consider(reading)
            self._rearm()

    @property
    def _is_gated(self) -> bool:
        return not (self._is_initialized and self._is_mesg_on)

    def _gate_closed(self) -> bool:
        """Whether no event may go out: no link, not initialised, or mesg off."""
        return self._link is None or self._is_gated

    def _is_asleep(self, reading: _Reading, *, frees: bool) -> bool:
        """Whether *reading*'s kind timed out twice and nothing has woken it."""
        asleep = self._dormant.get(reading.kind)
        if asleep is None:
            return False
        if frees or asleep != reading.fingerprint:
            del self._dormant[reading.kind]
            return False
        return True

    async def _consider(self, reading: _Reading) -> None:
        """Send, hold or drop one kind according to the rate window."""
        if not reading.is_owed:
            if reading.is_newer:
                reading.commit()  # nothing is owed: read, expired or withdrawn
            self._release(reading.kind)
            return
        wait = self._rate.opens_in(reading.kind)
        if wait > 0:
            self._due[reading.kind] = self._clock() + wait
            return
        await self._send(reading)

    def _release(self, kind: EventKind) -> None:
        """Stop holding *kind*; its record is unchanged."""
        self._due.pop(kind, None)
        self._retried.discard(kind)

    async def _send(self, reading: _Reading) -> None:
        """Send *reading*'s event and advance the record only if the send completes."""
        link, event, kind = self._link, reading.event, reading.kind
        if link is None or event is None:
            return
        label = f"channel event ({kind.value})"
        try:
            await link.send(event.content, event.meta)
        except TimeoutError as exc:
            self._log.failed(label, exc)
            self._timed_out(reading)
        except Exception as exc:  # a closed link arms nothing; a bug logs at ERROR
            logger.debug("%s failed", label, exc_info=True)
            self._log.failed(label, exc)
            self._release(kind)
        else:
            reading.commit()
            self._rate.record(kind)
            self._release(kind)
            self._log.recovered(label)

    def _timed_out(self, reading: _Reading) -> None:
        """Hold the kind for one more try; a second timeout puts it to sleep."""
        kind = reading.kind
        if kind in self._retried:
            self._release(kind)
            self._dormant[kind] = reading.fingerprint
        else:
            self._retried.add(kind)
            self._due[kind] = self._clock() + RETRY_SECONDS

    # -- The one timer -------------------------------------------------------

    def _rearm(self) -> None:
        """Arm the deferred send for the earliest held kind; cancel it if none."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if not self._due or self._link is None or not self._link.is_open:
            return
        delay = max(min(self._due.values()) - self._clock(), 0.0)
        self._timer = asyncio.get_running_loop().call_later(delay, self._fire)

    def _fire(self) -> None:
        self._timer_task = asyncio.ensure_future(self._run_deferred())

    async def _run_deferred(self) -> None:
        try:
            await self.deferred_send()
        except Exception as exc:  # a bug in the sender, not a transport failure
            logger.debug("deferred send failed", exc_info=True)
            self._log.failed("channel deferred send", exc)

    # -- Reading what is true now --------------------------------------------

    async def _read(self, kind: EventKind) -> _Reading | None:
        """Read *kind* from the relay and talk state; ``None`` if the read failed."""
        label = f"channel read ({kind.value})"
        try:
            reading = await self._reading(kind)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("%s failed", label, exc_info=True)
            self._log.failed(label, exc)
            return None
        self._log.recovered(label)
        return reading

    async def _reading(self, kind: EventKind) -> _Reading:
        match kind:
            case EventKind.MAIL:
                return await self._mail()
            case EventKind.WALL:
                return await self._wall()
            case EventKind.TALK_INVITE:
                return self._invite()
            case EventKind.TALK_MESSAGE:
                return self._message()

    async def _mail(self) -> _Reading:
        """Mail is newer when the high-water mark passes the last-sent one."""
        source = self._source
        mark = await source.relay.inbox_high_water(source.session_key)
        is_newer = mark > self._mail_sent
        event = None
        if is_newer:  # the count is only the N of the text, read after the mark
            count = (await source.relay.get_unread_summary(source.session_key)).count
            event = ChannelEvent.mail(count) if count > 0 else None
        return _Reading(
            EventKind.MAIL, mark, is_newer, event, lambda: self._sent_mail(mark)
        )

    def _sent_mail(self, mark: int) -> None:
        self._mail_sent = max(self._mail_sent, mark)

    async def _wall(self) -> _Reading:
        """A wall is newer when its key was never announced, and is not our own."""
        wall = await self._source.relay.get_wall()
        if wall is None or wall.is_expired:
            return _Reading(
                kind=EventKind.WALL,
                fingerprint=None,
                is_newer=False,
                event=None,
                commit=_nothing,
            )
        key: WallKey = (wall.posted_at, wall.from_user, wall.from_tty)
        event = None if self._is_own(wall) else self._wall_event(wall)
        return _Reading(
            EventKind.WALL,
            key,
            key not in self._walls_sent,
            event,
            lambda: self._walls_sent.add(key),
        )

    def _wall_event(self, wall: WallPost) -> ChannelEvent:
        user = self._source.session_key.partition(":")[0]
        sender = Sender.claimed(wall.from_user, wall.from_tty, own_user=user)
        return ChannelEvent.wall(sender)

    def _is_own(self, wall: WallPost) -> bool:
        """Whether this session posted *wall* (by display name or routing tty)."""
        user, _, tty = self._source.session_key.partition(":")
        names = {tty, self._source.talk.my_tty_name}
        return wall.from_user == user and wall.from_tty in names

    def _invite(self) -> _Reading:
        """An invite is newer when an arrival was counted that was not announced."""
        talk = self._source.talk
        count = talk.invite_arrivals
        inviter = talk.latest_inviter() if talk.has_live_invite() else None
        event = None
        if inviter is not None:
            user = self._source.session_key.partition(":")[0]
            sender = Sender.claimed(inviter[0], inviter[1], own_user=user)
            event = ChannelEvent.talk_invite(sender)
        return _Reading(
            EventKind.TALK_INVITE,
            count,
            count > self._invites_sent,
            event,
            lambda: self._sent_invite(count),
        )

    def _sent_invite(self, count: int) -> None:
        self._invites_sent = max(self._invites_sent, count)

    def _message(self) -> _Reading:
        """A talk message is newer when a partner arrival was not announced."""
        talk = self._source.talk
        count = talk.message_arrivals
        event = ChannelEvent.talk_message() if talk.has_partner_message() else None
        return _Reading(
            EventKind.TALK_MESSAGE,
            count,
            count > self._messages_sent,
            event,
            lambda: self._sent_message(count),
        )

    def _sent_message(self, count: int) -> None:
        self._messages_sent = max(self._messages_sent, count)


def _nothing() -> None:
    """Advance no record."""
