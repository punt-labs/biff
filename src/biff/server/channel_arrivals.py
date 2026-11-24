"""Detect each kind of arrival, and remember what was last sent for it."""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Self

from biff.server.channel_content import ChannelEvent, EventKind, Sender
from biff.server.channel_reading import ArrivalSource, Reading
from biff.talk_types import TalkNotification, TalkPhase

if TYPE_CHECKING:
    from biff.models import WallPost

type WallKey = tuple[datetime, str, str]

_INVITE_LOG = 256  # accepted invites remembered, to name a live one


class Arrivals:
    """Read mail, walls and talk from the source, and keep the last-sent records.

    A record advances only through a reading's ``commit``, to the value the
    reading took before the send.  Mail is ordered by the relay's high-water
    mark, never by the unread count; a wall by its ``(posted_at, from_user,
    from_tty)`` key; an invite and a talk message by counters that rise when
    the frame is accepted and never fall.
    """

    _source: ArrivalSource
    _mail_sent: int
    _invites_sent: int
    _messages_sent: int
    _walls_sent: set[WallKey]
    _invites_seen: int
    _messages_seen: int
    _invites: deque[tuple[int, str, str, str]]

    def __new__(cls, source: ArrivalSource) -> Self:
        """Create records in which nothing has been sent or seen."""
        self = super().__new__(cls)
        self._source = source
        self._mail_sent = 0
        self._invites_sent = 0
        self._messages_sent = 0
        self._walls_sent = set()
        self._invites_seen = 0
        self._messages_seen = 0
        self._invites = deque(maxlen=_INVITE_LOG)
        return self

    def count(self, notif: TalkNotification) -> None:
        """Count an accepted talk frame: an invite, or a message from the partner.

        A message counts only from the connected partner (the DES-046 key
        check); a peer session that invites again counts again.
        """
        talk = self._source.talk
        if notif.is_invite:
            self._invites_seen += 1
            self._invites.append(
                (self._invites_seen, notif.nfrom_key, notif.nfrom, notif.nfrom_tty)
            )
        elif (
            notif.ntype == "message"
            and talk.phase is TalkPhase.CONNECTED
            and notif.nfrom_key == talk.partner_key
        ):
            self._messages_seen += 1

    async def reading(self, kind: EventKind) -> Reading:
        """Read *kind* now: its fingerprint, whether it is newer, and its event."""
        match kind:
            case EventKind.MAIL:
                return await self._mail()
            case EventKind.WALL:
                return await self._wall()
            case EventKind.TALK_INVITE:
                return self._invite()
            case EventKind.TALK_MESSAGE:
                return self._message()

    async def _mail(self) -> Reading:
        """Mail is newer when the high-water mark passes the last-sent one."""
        source = self._source
        mark = await source.relay.inbox_high_water(source.session_key)
        is_newer = mark > self._mail_sent
        event = None
        if is_newer:  # the count is only the N of the text, read after the mark
            count = (await source.relay.get_unread_summary(source.session_key)).count
            event = ChannelEvent.mail(count) if count > 0 else None
        return Reading(
            EventKind.MAIL, mark, is_newer, event, lambda: self._sent_mail(mark)
        )

    def _sent_mail(self, mark: int) -> None:
        self._mail_sent = max(self._mail_sent, mark)

    async def _wall(self) -> Reading:
        """Read the wall: newer when its key was never announced, and not our own."""
        wall = await self._source.relay.get_wall()
        if wall is None or wall.is_expired:
            return Reading(
                kind=EventKind.WALL,
                fingerprint=None,
                is_newer=False,
                event=None,
                commit=_nothing,
            )
        key: WallKey = (wall.posted_at, wall.from_user, wall.from_tty)
        event = None if self._is_own(wall) else self._wall_event(wall)
        return Reading(
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

    def _invite(self) -> Reading:
        """Read invites: newer when a live one arrived after the last announcement.

        The sender comes from an invite that is still queued or pending, never
        from one that was withdrawn or has expired.
        """
        talk = self._source.talk
        frames, pending = talk.peek()
        live = {n.nfrom_key for n in frames if n.is_invite}
        live |= {p.session_key for p in pending.values()}
        newest = max(
            (entry for entry in self._invites if entry[1] in live),
            key=lambda entry: entry[0],
            default=None,
        )
        count = self._invites_seen
        event = None
        if newest is not None:
            user = self._source.session_key.partition(":")[0]
            event = ChannelEvent.talk_invite(
                Sender.claimed(newest[2], newest[3], own_user=user)
            )
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
        """Read talk messages: newer when a partner arrival was never announced."""
        talk = self._source.talk
        count = self._messages_seen
        frames = talk.peek()[0] if talk.phase is TalkPhase.CONNECTED else ()
        is_queued = any(
            n.ntype == "message" and n.nfrom_key == talk.partner_key for n in frames
        )
        event = ChannelEvent.talk_message() if is_queued else None
        return Reading(
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
