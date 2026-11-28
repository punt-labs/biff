"""What has arrived for a session, as biffd reports it: snapshots and events."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, cast, final

from biff.models import WallPost
from biff.server.channel_content import ChannelEvent, EventKind, Sender
from biff.server.channel_gate import Gate
from biff.server.channel_reading import Reading

if TYPE_CHECKING:
    from datetime import datetime

    from biff.frames import Event

__all__ = ["ArrivalsSnapshot", "DaemonArrivals", "DaemonGate"]

type WallKey = tuple[datetime, str, str]

_INVITE_LOG = 256  # accepted invites remembered, to name a live one


@final
@dataclass(frozen=True, slots=True)
class ArrivalsSnapshot:
    """The reply to biffd's ``arrivals``: what is true of the session now."""

    mark: int
    unread: int
    wall: WallPost | None
    invites: tuple[tuple[str, str], ...]
    talk_pending: int
    mesg_on: bool

    @classmethod
    def of(cls, data: dict[str, object]) -> Self:
        """Return the snapshot *data*, the reply's ``data``, describes.

        Raises:
            ValueError: *data* is not an ``arrivals`` reply.
        """
        try:
            wall = data["wall"]
            invites = cast("list[dict[str, str]]", data["invites"])
            return cls(
                mark=int(cast("int", data["mark"])),
                unread=int(cast("int", data["unread"])),
                wall=None if wall is None else WallPost.model_validate(wall),
                invites=tuple((i["from"], i["tty"]) for i in invites),
                talk_pending=int(cast("int", data["talk_pending"])),
                mesg_on=bool(data.get("mesg", True)),
            )
        except (KeyError, TypeError) as exc:
            msg = f"biffd answered arrivals with {sorted(data)}"
            raise ValueError(msg) from exc


@final
class DaemonArrivals:
    """Detects each kind of arrival from biffd's snapshot and its talk events.

    The same detection as the in-process reader: mail is ordered by the inbox's
    high-water mark, a wall by its key, an invite and a talk message by counters
    that rise as biffd's events arrive and never fall.  The last-sent records
    advance only through a reading's ``commit``.
    """

    __slots__ = (
        "_invites",
        "_invites_seen",
        "_invites_sent",
        "_mail_sent",
        "_messages_seen",
        "_messages_sent",
        "_names",
        "_snapshot",
        "_user",
        "_walls_sent",
    )

    _snapshot: ArrivalsSnapshot | None
    _user: str
    _names: frozenset[str]
    _mail_sent: int
    _walls_sent: set[WallKey]
    _invites_seen: int
    _invites_sent: int
    _messages_seen: int
    _messages_sent: int
    _invites: deque[tuple[int, str, str]]

    def __new__(cls) -> Self:
        """Create a reader in which nothing has been sent or seen."""
        self = super().__new__(cls)
        self._snapshot = None
        self._user = ""
        self._names = frozenset()
        self._mail_sent = 0
        self._walls_sent = set()
        self._invites_seen = 0
        self._invites_sent = 0
        self._messages_seen = 0
        self._messages_sent = 0
        self._invites = deque(maxlen=_INVITE_LOG)
        return self

    @property
    def mesg_on(self) -> bool | None:
        """Whether the session takes messages; ``None`` before the first snapshot."""
        return None if self._snapshot is None else self._snapshot.mesg_on

    def is_whom(self, user: str, names: frozenset[str]) -> None:
        """Say which user, and which tty names, the session's own wall carries."""
        self._user = user
        self._names = names

    def refresh(self, snapshot: ArrivalsSnapshot) -> None:
        """Take *snapshot* as what is true now."""
        self._snapshot = snapshot

    def count(self, event: Event) -> None:
        """Count a talk event: an invite, or a line from the partner.

        biffd raises ``talk_line`` only for the accepted partner, so every line
        counts; an invite from a session that invites again counts again.
        """
        data = cast("dict[str, object]", event.data or {})
        if event.kind == "talk_invite":
            self._invites_seen += 1
            self._invites.append(
                (
                    self._invites_seen,
                    str(data.get("from", "")),
                    str(data.get("tty", "")),
                )
            )
        elif event.kind == "talk_line":
            self._messages_seen += 1

    async def reading(self, kind: EventKind) -> Reading:
        """Read *kind* from the last snapshot: fingerprint, newer, and its event."""
        match kind:
            case EventKind.MAIL:
                return self._mail()
            case EventKind.WALL:
                return self._wall()
            case EventKind.TALK_INVITE:
                return self._invite()
            case EventKind.TALK_MESSAGE:
                return self._message()

    def _mail(self) -> Reading:
        snap = self._snapshot
        mark = 0 if snap is None else snap.mark
        count = 0 if snap is None else snap.unread
        is_newer = mark > self._mail_sent
        event = ChannelEvent.mail(count) if is_newer and count > 0 else None
        return Reading(EventKind.MAIL, mark, is_newer, event, lambda: self._sent(mark))

    def _sent(self, mark: int) -> None:
        self._mail_sent = max(self._mail_sent, mark)

    def _wall(self) -> Reading:
        wall = None if self._snapshot is None else self._snapshot.wall
        if wall is None or wall.is_expired:
            return Reading(
                kind=EventKind.WALL,
                fingerprint=None,
                is_newer=False,
                event=None,
                commit=_nothing,
            )
        key: WallKey = (wall.posted_at, wall.from_user, wall.from_tty)
        own = wall.from_user == self._user and wall.from_tty in self._names
        event = (
            None
            if own
            else ChannelEvent.wall(Sender.claimed(wall.from_user, wall.from_tty))
        )
        return Reading(
            EventKind.WALL,
            key,
            key not in self._walls_sent,
            event,
            lambda: self._walls_sent.add(key),
        )

    def _invite(self) -> Reading:
        live: set[tuple[str, str]] = (
            set() if self._snapshot is None else set(self._snapshot.invites)
        )
        newest = max(
            (entry for entry in self._invites if (entry[1], entry[2]) in live),
            key=lambda entry: entry[0],
            default=None,
        )
        count = self._invites_seen
        event = None
        if newest is not None:
            event = ChannelEvent.talk_invite(Sender.claimed(newest[1], newest[2]))
        return Reading(
            EventKind.TALK_INVITE,
            count,
            newest is not None and newest[0] > self._invites_sent,
            event,
            lambda: self._sent_invite(count),
        )

    def _sent_invite(self, count: int) -> None:
        self._invites_sent = max(self._invites_sent, count)

    def _message(self) -> Reading:
        count = self._messages_seen
        queued = self._snapshot is not None and self._snapshot.talk_pending > 0
        event = ChannelEvent.talk_message() if queued else None
        return Reading(
            EventKind.TALK_MESSAGE,
            count,
            count > self._messages_sent,
            event,
            lambda: self._sent_message(count),
        )

    def _sent_message(self, count: int) -> None:
        self._messages_sent = max(self._messages_sent, count)


@final
class DaemonGate:
    """The client's readiness and the user's mesg, as the last snapshot says."""

    __slots__ = ("_arrivals", "_is_initialized")

    _arrivals: DaemonArrivals
    _is_initialized: bool

    def __new__(cls, arrivals: DaemonArrivals) -> Self:
        """Create a gate closed until the client says it is initialised."""
        self = super().__new__(cls)
        self._arrivals = arrivals
        self._is_initialized = False
        return self

    def initializing(self) -> None:
        """Close the gate: the client sent ``initialize`` and is not yet ready."""
        self._is_initialized = False

    def initialized(self) -> None:
        """Open the gate to the client: it sent ``notifications/initialized``."""
        self._is_initialized = True

    async def state(self) -> Gate:
        """Say whether an event may go out: ready, mesg on, a snapshot taken."""
        if not self._is_initialized:
            return Gate.CLOSED
        mesg = self._arrivals.mesg_on
        if mesg is None:
            return Gate.UNREADABLE  # biffd has not answered yet: try again
        return Gate.OPEN if mesg else Gate.CLOSED


def _nothing() -> None:
    """Advance no record."""
