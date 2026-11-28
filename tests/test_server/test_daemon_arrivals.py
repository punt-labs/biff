"""DaemonArrivals detects what the in-process reader does, from biffd's reports."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from biff.frames import Event
from biff.models import WallPost
from biff.server.channel_content import EventKind
from biff.server.channel_emitter import ChannelEmitter
from biff.server.channel_gate import Gate
from biff.server.daemon_arrivals import ArrivalsSnapshot, DaemonArrivals, DaemonGate


class _Sink:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, str]]] = []

    @property
    def is_open(self) -> bool:
        return True

    async def send(self, content: str, meta: dict[str, str]) -> None:
        self.sent.append((content, dict(meta)))


def _snapshot(
    *,
    mark: int = 0,
    unread: int = 0,
    wall: WallPost | None = None,
    invites: tuple[tuple[str, str], ...] = (),
    talk_pending: int = 0,
    talk_lines: int = 0,
    mesg_on: bool = True,
) -> ArrivalsSnapshot:
    return ArrivalsSnapshot(
        mark=mark,
        unread=unread,
        wall=wall,
        invites=invites,
        talk_pending=talk_pending,
        talk_lines=talk_lines,
        mesg_on=mesg_on,
    )


def _wall(user: str = "eric", tty: str = "tty1") -> WallPost:
    return WallPost(
        text="deploy freeze",
        from_user=user,
        from_tty=tty,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def _event(kind: str, sender: str = "eric", tty: str = "tty1") -> Event:
    return Event(
        seq=1,
        kind=kind,  # type: ignore[arg-type]
        data={"from": sender, "tty": tty, "pending": 1},
    )


async def test_mail_is_owed_when_the_mark_passes_the_last_sent_one() -> None:
    arrivals = DaemonArrivals()
    arrivals.refresh(_snapshot(mark=3, unread=2))

    reading = await arrivals.reading(EventKind.MAIL)

    assert reading.is_owed
    assert reading.event is not None
    assert "2" in reading.event.content
    reading.commit()
    assert not (await arrivals.reading(EventKind.MAIL)).is_newer


async def test_a_read_that_lowers_the_unread_count_is_not_an_arrival() -> None:
    arrivals = DaemonArrivals()
    arrivals.refresh(_snapshot(mark=3, unread=2))
    (await arrivals.reading(EventKind.MAIL)).commit()

    arrivals.refresh(_snapshot(mark=3, unread=0))

    assert not (await arrivals.reading(EventKind.MAIL)).is_owed


async def test_a_wall_is_owed_once_and_not_for_its_own_poster() -> None:
    arrivals = DaemonArrivals()
    arrivals.is_whom("kai", frozenset({"tty2"}))
    arrivals.refresh(_snapshot(wall=_wall()))
    reading = await arrivals.reading(EventKind.WALL)
    assert reading.is_owed
    reading.commit()
    assert not (await arrivals.reading(EventKind.WALL)).is_newer

    arrivals.refresh(_snapshot(wall=_wall("kai", "tty2")))

    assert (await arrivals.reading(EventKind.WALL)).event is None


async def test_an_invite_is_owed_while_it_is_live_and_not_after_it_is_withdrawn() -> (
    None
):
    arrivals = DaemonArrivals()
    arrivals.count(_event("talk_invite"))
    arrivals.refresh(_snapshot(invites=(("eric", "tty1"),)))
    assert (await arrivals.reading(EventKind.TALK_INVITE)).is_owed

    arrivals.refresh(_snapshot(invites=()))

    assert not (await arrivals.reading(EventKind.TALK_INVITE)).is_owed


async def test_an_invite_that_arrives_again_is_owed_again() -> None:
    arrivals = DaemonArrivals()
    arrivals.count(_event("talk_invite"))
    arrivals.refresh(_snapshot(invites=(("eric", "tty1"),)))
    (await arrivals.reading(EventKind.TALK_INVITE)).commit()
    arrivals.count(_event("talk_invite"))

    assert (await arrivals.reading(EventKind.TALK_INVITE)).is_owed


async def test_a_talk_line_is_owed_while_something_waits() -> None:
    arrivals = DaemonArrivals()
    arrivals.count(_event("talk_line"))
    arrivals.refresh(_snapshot(talk_pending=1, talk_lines=1))
    reading = await arrivals.reading(EventKind.TALK_MESSAGE)
    assert reading.is_owed
    reading.commit()

    arrivals.refresh(_snapshot(talk_pending=0, talk_lines=0))
    arrivals.count(_event("talk_line"))

    assert not (await arrivals.reading(EventKind.TALK_MESSAGE)).is_owed


@pytest.mark.parametrize(
    ("initialized", "snapshot", "expected"),
    [
        (False, _snapshot(), Gate.CLOSED),
        (True, None, Gate.UNREADABLE),
        (True, _snapshot(mesg_on=False), Gate.CLOSED),
        (True, _snapshot(mesg_on=True), Gate.OPEN),
    ],
)
async def test_the_gate_needs_an_initialised_client_a_snapshot_and_mesg_on(
    initialized: bool, snapshot: ArrivalsSnapshot | None, expected: Gate
) -> None:
    arrivals = DaemonArrivals()
    if snapshot is not None:
        arrivals.refresh(snapshot)
    gate = DaemonGate(arrivals)
    if initialized:
        gate.initialized()

    assert await gate.state() is expected


async def test_an_emitter_with_no_relay_sends_mail_from_the_daemon_reader() -> None:
    arrivals = DaemonArrivals()
    gate = DaemonGate(arrivals)
    emitter = ChannelEmitter(None, lambda: None, arrivals=arrivals, gate=gate)
    sink = _Sink()
    emitter.serve(sink)  # type: ignore[arg-type]
    gate.initialized()
    arrivals.refresh(_snapshot(mark=1, unread=1))

    await emitter.check()

    assert len(sink.sent) == 1
    assert sink.sent[0][1]["kind"] == "mail"


def test_an_emitter_with_no_source_needs_its_own_reader() -> None:
    with pytest.raises(ValueError, match="own arrivals"):
        ChannelEmitter(None, lambda: None)


def test_a_malformed_wall_is_logged_and_skipped_not_fatal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    data: dict[str, object] = {
        "mark": 1,
        "unread": 0,
        "wall": {"text": "", "from_user": "eric"},  # no text, no expiry
        "invites": [],
        "talk_pending": 0,
        "talk_lines": 0,
        "mesg": True,
    }

    with caplog.at_level(logging.WARNING):
        snapshot = ArrivalsSnapshot.of(data)

    assert snapshot.wall is None
    assert snapshot.mark == 1  # the rest of the snapshot is kept
    assert "malformed wall" in caplog.text


def test_forgetting_a_session_clears_what_was_sent_and_what_was_known() -> None:
    arrivals = DaemonArrivals()
    arrivals.refresh(_snapshot(mark=5, unread=1, mesg_on=False))

    arrivals.forget()

    assert arrivals.mesg_on is None  # no snapshot of the new session yet


async def test_an_invite_queued_before_the_feed_subscribed_is_owed() -> None:
    arrivals = DaemonArrivals()

    arrivals.refresh(_snapshot(invites=(("eric", "tty1"),), talk_pending=1))

    reading = await arrivals.reading(EventKind.TALK_INVITE)
    assert reading.is_owed  # no talk_invite event was ever seen for it


async def test_an_invite_the_event_and_the_snapshot_both_report_is_owed_once() -> None:
    arrivals = DaemonArrivals()
    arrivals.refresh(_snapshot(invites=(("eric", "tty1"),)))
    (await arrivals.reading(EventKind.TALK_INVITE)).commit()

    arrivals.count(_event("talk_invite"))  # the event of the same invite, late
    arrivals.refresh(_snapshot(invites=(("eric", "tty1"),)))

    assert not (await arrivals.reading(EventKind.TALK_INVITE)).is_newer


async def test_an_invite_withdrawn_and_made_again_in_a_gap_is_owed_again() -> None:
    arrivals = DaemonArrivals()
    arrivals.refresh(_snapshot(invites=(("eric", "tty1"),)))
    (await arrivals.reading(EventKind.TALK_INVITE)).commit()
    arrivals.refresh(_snapshot(invites=()))  # withdrawn while no event was heard

    arrivals.refresh(_snapshot(invites=(("eric", "tty1"),)))  # and made again

    assert (await arrivals.reading(EventKind.TALK_INVITE)).is_owed


async def test_talk_lines_waiting_when_the_feed_subscribes_are_owed_once() -> None:
    arrivals = DaemonArrivals()
    arrivals.refresh(_snapshot(talk_pending=2, talk_lines=2))
    arrivals.subscribed()
    reading = await arrivals.reading(EventKind.TALK_MESSAGE)
    assert reading.is_owed
    reading.commit()

    arrivals.subscribed()  # a reconnect with the same lines still unread

    assert not (await arrivals.reading(EventKind.TALK_MESSAGE)).is_newer

    arrivals.refresh(_snapshot(talk_pending=0, talk_lines=0))  # read
    arrivals.refresh(_snapshot(talk_pending=1, talk_lines=1))  # a line, in a gap
    arrivals.subscribed()

    assert (await arrivals.reading(EventKind.TALK_MESSAGE)).is_owed


async def test_a_queued_invite_alone_is_not_a_talk_line_waiting() -> None:
    arrivals = DaemonArrivals()
    # biffd's queue holds the invite frame: one frame queued, no line among them.
    arrivals.refresh(
        _snapshot(invites=(("eric", "tty1"),), talk_pending=1, talk_lines=0)
    )

    arrivals.subscribed()

    assert not (await arrivals.reading(EventKind.TALK_MESSAGE)).is_owed
