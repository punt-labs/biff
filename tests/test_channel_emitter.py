"""The channel emitter: detection, content, record, rate and the deferred send.

Each test names the obligation of ``docs/notification.tex`` ("What the
implementation owes") it proves, as ``O<n>``.  The relay is the real local
relay; the client is a real ``ChannelLink`` over a write stream the test
controls, because a fake link that raises would prove nothing about what the
SDK does with a stalled write.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from biff.models import Message, UnreadSummary, WallPost
from biff.relay import LocalRelay
from biff.server import channel
from biff.server.channel import ChannelLink
from biff.server.channel_content import EventKind
from biff.server.channel_emitter import ChannelEmitter
from biff.server.channel_rate import RateLimit
from biff.talk_state import PENDING_INVITE_TTL, TalkState

if TYPE_CHECKING:
    from mcp.shared.message import SessionMessage

_ME = "kai:abc12345"
_PEER = "eric:def67890"


class _Clock:
    """A monotonic clock the test moves."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Sink:
    """The client's end of the write stream: records frames, can stall or fail."""

    def __init__(self) -> None:
        self.frames: list[dict[str, object]] = []
        self.stall = False
        self.on_send: list[object] = []

    async def send(self, item: SessionMessage, /) -> None:
        if self.stall:
            await asyncio.sleep(3600)
        for hook in self.on_send:
            if callable(hook):
                await hook()
        self.frames.append(json.loads(item.message.model_dump_json()))

    async def aclose(self) -> None:
        return None

    @property
    def contents(self) -> list[str]:
        return [str(_params(f)["content"]) for f in self.frames]

    @property
    def metas(self) -> list[dict[str, str]]:
        return [cast("dict[str, str]", _params(f)["meta"]) for f in self.frames]


def _params(frame: dict[str, object]) -> dict[str, object]:
    return cast("dict[str, object]", frame["params"])


class _CountingRelay(LocalRelay):
    """A local relay whose unread count a test can script, and whose reads it counts."""

    scripted: list[int] | None = None
    reads = 0
    fails_with: Exception | None = None

    async def inbox_high_water(self, session_key: str) -> int:
        self.reads += 1
        if self.fails_with is not None:
            raise self.fails_with
        return await super().inbox_high_water(session_key)

    async def get_unread_summary(self, session_key: str) -> UnreadSummary:
        if self.scripted:
            return UnreadSummary(count=self.scripted.pop(0))
        return await super().get_unread_summary(session_key)


@dataclass
class _Source:
    relay: _CountingRelay
    talk: TalkState
    session_key: str = _ME


@dataclass
class _Rig:
    emitter: ChannelEmitter
    source: _Source
    sink: _Sink
    clock: _Clock
    link: ChannelLink
    wakes: list[int] = field(default_factory=list)

    async def mail(self, n: int = 1, *, to: str = _ME, sender: str = "eric") -> None:
        for i in range(n):
            await self.source.relay.deliver(
                Message(from_user=sender, to_user=to, body=f"SECRET-BODY-{i}")
            )

    async def wall(
        self, *, user: str = "eric", tty: str = "tty9", text: str = "SECRET-WALL"
    ) -> WallPost:
        post = WallPost(
            text=text,
            from_user=user,
            from_tty=tty,
            posted_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        await self.source.relay.set_wall(post)
        return post

    def invite(
        self, *, user: str = "eric", tty: str = "tty9", key: str = _PEER
    ) -> None:
        self.source.talk.receive(
            {
                "type": "invite",
                "from": user,
                "from_tty": tty,
                "from_key": key,
                "to_key": _ME,
                "body": "SECRET-INVITE",
            }
        )

    def open(self) -> None:
        self.emitter.initialized()


@pytest.fixture
def rig(tmp_path: Path) -> _Rig:
    relay = _CountingRelay(data_dir=tmp_path)
    talk = TalkState(
        relay=relay, user="kai", tty="abc12345", session_key=_ME, tty_name="tty1"
    )
    source = _Source(relay, talk)
    wakes: list[int] = []
    clock = _Clock()
    emitter = ChannelEmitter(source, lambda: wakes.append(1), clock=clock)
    sink = _Sink()
    link = ChannelLink()
    link.observe(sink)
    emitter.serve(link)
    return _Rig(emitter, source, sink, clock, link, wakes)


async def _quiet(rig: _Rig) -> None:
    """Let any armed timer's task finish, as the loop would."""
    await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def _short_send_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(channel, "CHANNEL_SEND_TIMEOUT", 0.05)


# -- O1: the initialisation gate ------------------------------------------------


class TestInitialisationGate:
    async def test_a_wall_detected_before_initialized_is_sent_after_it(
        self, rig: _Rig
    ) -> None:
        """O1."""
        await rig.wall()

        await rig.emitter.check()
        assert rig.sink.frames == []

        rig.open()
        await rig.emitter.check()
        assert rig.sink.metas == [{"kind": "wall"}]

    async def test_nothing_is_read_before_initialized(self, rig: _Rig) -> None:
        """O1: a gated pass touches no relay."""
        await rig.mail()
        await rig.emitter.check()
        assert rig.source.relay.reads == 0

    async def test_a_second_initialize_closes_the_gate_until_initialized(
        self, rig: _Rig
    ) -> None:
        """O1."""
        rig.open()
        await rig.mail()
        rig.emitter.initializing()

        await rig.emitter.check()
        assert rig.sink.frames == []

        rig.open()
        await rig.emitter.check()
        assert len(rig.sink.frames) == 1

    async def test_initialized_asks_the_poller_for_a_pass(self, rig: _Rig) -> None:
        """O1: the gate opening is an arrival; the poller runs the sender."""
        rig.open()
        assert rig.wakes == [1]


# -- O2: mesg -------------------------------------------------------------------


class TestMesg:
    async def test_mesg_off_sends_nothing_and_on_sends_current_state_once(
        self, rig: _Rig
    ) -> None:
        """O2."""
        rig.open()
        rig.emitter.set_mesg(enabled=False)
        await rig.mail(2)
        await rig.wall()

        await rig.emitter.check()
        assert rig.sink.frames == []

        rig.emitter.set_mesg(enabled=True)
        await rig.emitter.check()
        assert sorted(m["kind"] for m in rig.sink.metas) == ["mail", "wall"]

        await rig.emitter.check()
        assert len(rig.sink.frames) == 2

    async def test_mesg_on_asks_the_poller_for_a_pass(self, rig: _Rig) -> None:
        """O2."""
        rig.emitter.set_mesg(enabled=False)
        rig.wakes.clear()
        rig.emitter.set_mesg(enabled=True)
        assert rig.wakes == [1]


# -- O3: owed is derived, per kind ----------------------------------------------


class TestMail:
    async def test_ten_messages_in_one_window_are_one_event_with_count_ten(
        self, rig: _Rig
    ) -> None:
        """O3, O6."""
        rig.open()
        await rig.mail()
        await rig.emitter.check()
        await rig.mail(9)
        await rig.emitter.check()  # inside the window: held, not sent
        assert len(rig.sink.frames) == 1

        rig.clock.advance(31)
        await rig.emitter.deferred_send()

        assert rig.sink.metas[-1] == {"kind": "mail", "count": "10"}

    async def test_mail_is_detected_when_the_unread_count_is_flat(
        self, rig: _Rig
    ) -> None:
        """O3: two read, two arrive."""
        rig.open()
        await rig.mail(2)
        await rig.emitter.check()
        keys = [m.id for m in await rig.source.relay.fetch(_ME)]
        await rig.source.relay.mark_read(_ME, keys)
        await rig.mail(2)
        rig.clock.advance(31)

        await rig.emitter.check()

        assert rig.sink.metas == [
            {"kind": "mail", "count": "2"},
            {"kind": "mail", "count": "2"},
        ]

    async def test_a_flapping_count_does_not_hide_or_repeat_an_arrival(
        self, rig: _Rig
    ) -> None:
        """O3: a stale fetch after a read reports 2, 1, 2; the mark is what counts."""
        rig.open()
        await rig.mail(2)
        rig.source.relay.scripted = [2, 1, 2]

        await rig.emitter.check()
        rig.clock.advance(31)
        await rig.emitter.check()  # the same arrivals: nothing new
        rig.clock.advance(31)
        await rig.emitter.check()

        assert rig.sink.metas == [{"kind": "mail", "count": "2"}]

    async def test_mail_names_no_sender_and_no_body(self, rig: _Rig) -> None:
        rig.open()
        await rig.mail(sender="mallory")
        await rig.emitter.check()
        assert "mallory" not in rig.sink.contents[0]
        assert "SECRET" not in rig.sink.contents[0]

    async def test_a_broadcast_mail_to_the_user_inbox_is_announced(
        self, rig: _Rig
    ) -> None:
        rig.open()
        await rig.mail(to="kai")
        await rig.emitter.check()
        assert rig.sink.metas == [{"kind": "mail", "count": "1"}]


class TestWall:
    async def test_a_wall_names_a_claimed_sender_and_no_body(self, rig: _Rig) -> None:
        rig.open()
        await rig.wall(text="SECRET-WALL")
        await rig.emitter.check()
        assert rig.sink.contents == [
            "New biff wall from eric:tty9 (claimed, unverified). Call wall to see it."
        ]

    async def test_this_sessions_own_wall_emits_nothing(self, rig: _Rig) -> None:
        """O3."""
        rig.open()
        await rig.wall(user="kai", tty="tty1")
        await rig.emitter.check()
        assert rig.sink.frames == []

    async def test_a_countdown_only_change_emits_nothing_after_the_first(
        self, rig: _Rig
    ) -> None:
        """O3: the key is (posted_at, from_user, from_tty); time left is not in it."""
        rig.open()
        await rig.wall()
        await rig.emitter.check()
        rig.clock.advance(120)
        await rig.emitter.check()
        await rig.emitter.check()
        assert len(rig.sink.frames) == 1

    async def test_each_wall_key_is_announced_once(self, rig: _Rig) -> None:
        """O3."""
        rig.open()
        await rig.wall(text="first")
        await rig.emitter.check()
        rig.clock.advance(31)
        await rig.wall(text="second")  # a new post, a new posted_at
        await rig.emitter.check()
        await rig.emitter.check()
        assert [m["kind"] for m in rig.sink.metas] == ["wall", "wall"]

    async def test_a_wall_from_the_session_user_on_another_tty_claims_to_be_you(
        self, rig: _Rig
    ) -> None:
        """O3: the label for an identity equal to this session's."""
        rig.open()
        await rig.wall(user="kai", tty="tty7")
        await rig.emitter.check()
        assert "unverified (claims to be you)" in rig.sink.contents[0]

    async def test_a_128_character_sender_is_a_teammate(self, rig: _Rig) -> None:
        """O3."""
        rig.open()
        await rig.wall(user="e" * 128)
        await rig.emitter.check()
        assert rig.sink.contents[0].startswith("New biff wall from a teammate (claimed")


class TestTalk:
    async def test_an_invite_is_announced_once_and_says_ask_the_user(
        self, rig: _Rig
    ) -> None:
        """O3."""
        rig.open()
        rig.invite()
        await rig.emitter.check()
        await rig.emitter.check()
        assert rig.sink.metas == [{"kind": "talk_invite"}]
        assert "accept" not in rig.sink.contents[0].lower()
        assert "ask the user" in rig.sink.contents[0].lower()
        assert "SECRET" not in rig.sink.contents[0]

    async def test_queued_to_pending_is_not_a_new_invite(self, rig: _Rig) -> None:
        """O3."""
        rig.open()
        rig.invite()
        await rig.emitter.check()
        rig.source.talk.drain_for_agent()
        rig.clock.advance(31)
        await rig.emitter.check()
        assert len(rig.sink.frames) == 1

    async def test_the_same_session_inviting_again_is_announced_again(
        self, rig: _Rig
    ) -> None:
        """O3: a session key is not an arrival identity."""
        rig.open()
        rig.invite()
        await rig.emitter.check()
        rig.clock.advance(31)
        rig.invite()
        await rig.emitter.check()
        assert len(rig.sink.frames) == 2

    async def test_a_partner_message_is_announced_without_its_body(
        self, rig: _Rig
    ) -> None:
        """O3."""
        rig.open()
        rig.source.talk.begin_connected(
            partner="eric", partner_tty="tty9", partner_key=_PEER
        )
        rig.source.talk.receive(
            {
                "type": "message",
                "from": "eric",
                "from_key": _PEER,
                "to_key": _ME,
                "body": "SECRET-MSG",
            }
        )
        await rig.emitter.check()
        assert rig.sink.contents == [
            "New talk message from your talk partner. Call talk_read."
        ]

    async def test_a_message_from_another_session_is_not_announced(
        self, rig: _Rig
    ) -> None:
        """O3: after the partner-key check."""
        rig.open()
        rig.source.talk.begin_connected(
            partner="eric", partner_tty="tty9", partner_key=_PEER
        )
        rig.source.talk.receive(
            {
                "type": "message",
                "from": "x",
                "from_key": "mallory:1",
                "to_key": _ME,
                "body": "b",
            }
        )
        await rig.emitter.check()
        assert rig.sink.frames == []

    async def test_a_message_after_talk_read_drained_the_queue_is_announced(
        self, rig: _Rig
    ) -> None:
        """O3."""
        rig.open()
        rig.source.talk.begin_connected(
            partner="eric", partner_tty="tty9", partner_key=_PEER
        )
        frame = {
            "type": "message",
            "from": "eric",
            "from_key": _PEER,
            "to_key": _ME,
            "body": "b",
        }
        rig.source.talk.receive(frame)
        await rig.emitter.check()
        rig.source.talk.drain_for_agent()
        rig.clock.advance(31)
        rig.source.talk.receive(frame)
        await rig.emitter.check()
        assert len(rig.sink.frames) == 2


# -- O4: never stale ------------------------------------------------------------


class TestNeverStale:
    async def test_mail_read_before_the_send_is_not_announced(self, rig: _Rig) -> None:
        """O4."""
        rig.open()
        await rig.mail()
        for m in await rig.source.relay.fetch(_ME):
            await rig.source.relay.mark_read(_ME, [m.id])
        await rig.emitter.check()
        assert rig.sink.frames == []

    async def test_an_invite_past_its_ttl_is_not_announced(self, rig: _Rig) -> None:
        """O4: the rate window can hold an event longer than the TTL."""
        rig.open()
        rig.invite()
        await rig.emitter.check()  # announced; now a second one is held
        rig.clock.advance(1)
        rig.invite(key="eric:other")
        await rig.emitter.check()
        assert rig.emitter.held == {EventKind.TALK_INVITE}
        rig.source.talk.expire_stale_invites(now=_far_future())
        rig.clock.advance(31)

        await rig.emitter.deferred_send()

        assert len(rig.sink.frames) == 1
        assert rig.emitter.held == frozenset()

    async def test_a_drained_message_queue_is_not_announced(self, rig: _Rig) -> None:
        """O4."""
        rig.open()
        rig.source.talk.begin_connected(
            partner="eric", partner_tty="tty9", partner_key=_PEER
        )
        rig.source.talk.receive(
            {
                "type": "message",
                "from": "eric",
                "from_key": _PEER,
                "to_key": _ME,
                "body": "b",
            }
        )
        rig.source.talk.drain_for_agent()
        await rig.emitter.check()
        assert rig.sink.frames == []

    async def test_an_expired_wall_is_not_announced(self, rig: _Rig) -> None:
        """O4."""
        rig.open()
        await rig.source.relay.set_wall(
            WallPost.model_construct(
                text="old",
                from_user="eric",
                from_tty="tty9",
                posted_at=datetime.now(UTC) - timedelta(hours=2),
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        await rig.emitter.check()
        assert rig.sink.frames == []


def _far_future() -> float:
    import time

    return time.monotonic() + PENDING_INVITE_TTL + 5


# -- O5: the record -------------------------------------------------------------


class TestRecord:
    async def test_a_timed_out_send_leaves_the_record_and_is_retried(
        self, rig: _Rig
    ) -> None:
        """O5."""
        rig.open()
        await rig.mail()
        rig.sink.stall = True
        await rig.emitter.check()
        assert rig.sink.frames == []
        assert rig.emitter.held == {EventKind.MAIL}

        rig.sink.stall = False
        rig.clock.advance(2)
        await rig.emitter.deferred_send()

        assert rig.sink.metas == [{"kind": "mail", "count": "1"}]

    async def test_a_mail_arriving_during_the_send_survives_it(self, rig: _Rig) -> None:
        """O5: the record is the value read before the send, not at completion."""
        rig.open()
        await rig.mail()

        async def arrives_mid_send() -> None:
            rig.sink.on_send.clear()
            await rig.mail()

        rig.sink.on_send.append(arrives_mid_send)
        await rig.emitter.check()
        rig.clock.advance(31)
        await rig.emitter.check()

        assert [m["count"] for m in rig.sink.metas] == ["1", "2"]

    async def test_a_closed_link_leaves_the_record_so_a_later_send_goes_out(
        self, rig: _Rig
    ) -> None:
        """O5."""
        rig.open()
        await rig.mail()
        rig.link.detach()
        await rig.emitter.check()
        rig.link.observe(rig.sink)

        await rig.emitter.check()

        assert len(rig.sink.frames) == 1


# -- O6: the rate limit ---------------------------------------------------------


class TestRate:
    async def test_one_event_per_kind_per_thirty_seconds(self, rig: _Rig) -> None:
        """O6."""
        rig.open()
        await rig.mail()
        await rig.emitter.check()
        await rig.mail()
        await rig.emitter.check()
        assert len(rig.sink.frames) == 1

        rig.clock.advance(29)
        await rig.emitter.check()
        assert len(rig.sink.frames) == 1

        rig.clock.advance(2)
        await rig.emitter.check()
        assert len(rig.sink.frames) == 2

    async def test_other_kinds_are_not_held_by_one_kinds_window(
        self, rig: _Rig
    ) -> None:
        """O6."""
        rig.open()
        await rig.mail()
        await rig.emitter.check()
        await rig.wall()
        await rig.emitter.check()
        assert [m["kind"] for m in rig.sink.metas] == ["mail", "wall"]

    async def test_ten_events_in_ten_minutes_is_the_cap(self, rig: _Rig) -> None:
        """O6."""
        rig.open()
        for _ in range(12):
            await rig.mail()
            await rig.emitter.check()
            rig.clock.advance(31)
        assert len(rig.sink.frames) == 10

        rig.clock.advance(600)
        await rig.emitter.check()
        assert len(rig.sink.frames) == 11

    async def test_the_limit_is_configurable(self, rig: _Rig) -> None:
        """O6."""
        tight = ChannelEmitter(
            rig.source,
            rig.wakes.clear,
            limit=RateLimit(per_kind=5, limit=2, span=60),
            clock=rig.clock,
        )
        tight.serve(rig.link)
        tight.initialized()
        await rig.mail()
        await tight.check()
        await rig.mail()
        rig.clock.advance(6)
        await tight.check()
        assert len(rig.sink.frames) == 2


# -- O7, O8, O9: the one timer --------------------------------------------------


class TestTimer:
    async def test_no_timer_exists_until_an_event_is_held(self, rig: _Rig) -> None:
        """O7, O14."""
        rig.open()
        await rig.emitter.check()
        assert not rig.emitter.is_timer_armed()
        await rig.mail()
        await rig.emitter.check()
        assert not rig.emitter.is_timer_armed()  # sent, nothing held

    async def test_a_send_that_empties_the_held_set_cancels_the_timer(
        self, rig: _Rig
    ) -> None:
        """O7."""
        rig.open()
        await rig.mail()
        await rig.emitter.check()
        await rig.mail()
        await rig.emitter.check()
        assert rig.emitter.is_timer_armed()
        assert rig.emitter.held == {EventKind.MAIL}

        rig.clock.advance(31)
        await rig.emitter.check()  # another path sends the last held kind

        assert not rig.emitter.is_timer_armed()
        assert rig.emitter.held == frozenset()

    async def test_a_closed_link_arms_nothing(self, rig: _Rig) -> None:
        """O7."""
        rig.open()
        await rig.mail()
        await rig.emitter.check()
        rig.link.detach()
        await rig.mail()
        await rig.emitter.check()
        assert not rig.emitter.is_timer_armed()

    async def test_the_timer_really_fires_and_sends(self, rig: _Rig) -> None:
        """O7: with a real clock, the one-shot sends the held event."""
        real = ChannelEmitter(
            rig.source, lambda: None, limit=RateLimit(per_kind=0.05, limit=10, span=60)
        )
        real.serve(rig.link)
        real.initialized()
        await rig.mail()
        await real.check()
        await rig.mail()
        await real.check()
        assert real.is_timer_armed()

        await asyncio.sleep(0.3)

        assert [m["count"] for m in rig.sink.metas] == ["1", "2"]
        assert not real.is_timer_armed()

    async def test_firing_sends_a_held_kind(self, rig: _Rig) -> None:
        """O8: send."""
        rig.open()
        await rig.mail()
        await rig.emitter.check()
        await rig.mail()
        await rig.emitter.check()
        rig.clock.advance(31)
        await rig.emitter.deferred_send()
        assert len(rig.sink.frames) == 2

    async def test_firing_drops_a_kind_nothing_is_owed_for(self, rig: _Rig) -> None:
        """O8: drop."""
        rig.open()
        await rig.mail()
        await rig.emitter.check()
        await rig.mail()
        await rig.emitter.check()
        for m in await rig.source.relay.fetch(_ME):
            await rig.source.relay.mark_read(_ME, [m.id])
        rig.clock.advance(31)

        await rig.emitter.deferred_send()

        assert len(rig.sink.frames) == 1
        assert rig.emitter.held == frozenset()
        assert not rig.emitter.is_timer_armed()

    async def test_firing_while_mesg_is_off_releases_the_kind_with_its_record(
        self, rig: _Rig
    ) -> None:
        """O8: TimerGated."""
        rig.open()
        await rig.mail()
        await rig.emitter.check()
        await rig.mail()
        await rig.emitter.check()
        rig.emitter.set_mesg(enabled=False)
        rig.clock.advance(31)

        await rig.emitter.deferred_send()

        assert len(rig.sink.frames) == 1
        assert rig.emitter.held == frozenset()
        assert not rig.emitter.is_timer_armed()
        rig.emitter.set_mesg(enabled=True)
        await rig.emitter.check()
        assert len(rig.sink.frames) == 2  # still owed: the record never moved

    async def test_firing_before_initialize_releases_the_kind(self, rig: _Rig) -> None:
        """O8: TimerGated."""
        rig.open()
        await rig.mail()
        await rig.emitter.check()
        await rig.mail()
        await rig.emitter.check()
        rig.emitter.initializing()
        rig.clock.advance(31)

        await rig.emitter.deferred_send()

        assert rig.emitter.held == frozenset()
        rig.open()
        await rig.emitter.check()
        assert len(rig.sink.frames) == 2

    async def test_a_timeout_re_arms_once_and_a_second_drops_that_kind_alone(
        self, rig: _Rig
    ) -> None:
        """O8: the first timeout re-arms that kind; the second releases only it."""
        rig.open()
        await rig.mail()
        await rig.wall()
        await rig.emitter.check()
        await rig.mail()
        rig.clock.advance(1)
        await rig.wall(text="again")
        rig.clock.advance(31)
        rig.sink.stall = True
        await rig.emitter.check()  # both time out once
        assert rig.emitter.held == {EventKind.MAIL, EventKind.WALL}

        rig.clock.advance(2)
        await rig.emitter.deferred_send()  # both time out a second time
        assert rig.emitter.held == frozenset()
        assert not rig.emitter.is_timer_armed()

    async def test_a_second_timeout_of_one_kind_keeps_the_others_held(
        self, rig: _Rig
    ) -> None:
        """O8."""
        rig.open()
        await rig.mail()
        await rig.emitter.check()
        await rig.mail()
        rig.clock.advance(31)
        rig.sink.stall = True
        await rig.emitter.check()  # mail times out once
        rig.sink.stall = False
        await rig.wall()
        rig.clock.advance(1)
        rig.sink.stall = True
        await rig.emitter.check()  # wall times out once; mail twice
        assert rig.emitter.held == {EventKind.WALL}
        assert rig.emitter.is_timer_armed()


class TestRetry:
    async def _dormant_mail(self, rig: _Rig) -> None:
        rig.open()
        await rig.mail()
        rig.sink.stall = True
        await rig.emitter.check()
        rig.clock.advance(2)
        await rig.emitter.deferred_send()
        rig.sink.stall = False
        assert rig.emitter.held == frozenset()
        assert rig.sink.frames == []

    async def test_a_timer_firing_never_retries_a_dormant_kind(self, rig: _Rig) -> None:
        """O9."""
        await self._dormant_mail(rig)
        rig.clock.advance(100)
        await rig.emitter.deferred_send()
        assert rig.sink.frames == []

    async def test_a_poller_pass_with_no_arrival_leaves_it_dormant(
        self, rig: _Rig
    ) -> None:
        """O9: a liveness wake must not retry a stalled client every 5 s."""
        await self._dormant_mail(rig)
        await rig.emitter.check()
        assert rig.sink.frames == []

    async def test_a_later_arrival_retries_it(self, rig: _Rig) -> None:
        """O9."""
        await self._dormant_mail(rig)
        await rig.mail()
        await rig.emitter.check()
        assert rig.sink.metas == [{"kind": "mail", "count": "2"}]

    async def test_a_tool_call_retries_it(self, rig: _Rig) -> None:
        """O9."""
        await self._dormant_mail(rig)
        rig.wakes.clear()
        rig.emitter.tool_called()
        assert rig.wakes == [1]
        await rig.emitter.check()
        assert len(rig.sink.frames) == 1

    async def test_a_tool_call_with_nothing_dormant_does_not_wake_the_poller(
        self, rig: _Rig
    ) -> None:
        """O9: a tool call costs no pass when nothing is owed."""
        rig.open()
        rig.wakes.clear()
        rig.emitter.tool_called()
        assert rig.wakes == []

    async def test_a_reconnect_retries_it(self, rig: _Rig) -> None:
        """O9, O10."""
        await self._dormant_mail(rig)
        rig.emitter.reconnected()
        await rig.emitter.check()
        assert len(rig.sink.frames) == 1


# -- O10: reconnect catch-up ----------------------------------------------------


class TestReconnect:
    async def test_a_reconnect_announces_mail_that_arrived_while_disconnected(
        self, rig: _Rig
    ) -> None:
        """O10: the missed arrival."""
        rig.open()
        await rig.mail()
        rig.emitter.reconnected()
        await rig.emitter.check()
        assert rig.sink.metas == [{"kind": "mail", "count": "1"}]

    async def test_a_reconnect_announces_nothing_already_announced(
        self, rig: _Rig
    ) -> None:
        """O10: unread and live is not newer."""
        rig.open()
        await rig.mail()
        await rig.emitter.check()
        rig.clock.advance(31)
        rig.emitter.reconnected()
        await rig.emitter.check()
        assert len(rig.sink.frames) == 1

    async def test_a_reconnect_does_nothing_when_channel_mode_is_off(
        self, rig: _Rig
    ) -> None:
        """O10."""
        quiet = ChannelEmitter(rig.source, rig.wakes.clear)
        quiet.reconnected()
        assert quiet.held == frozenset()

    def test_the_nats_relay_reports_an_in_place_reconnect(self) -> None:
        """O10: the listener the emitter registers is called by the relay."""
        from biff.nats_relay import NatsRelay

        relay = NatsRelay(url="nats://127.0.0.1:1", repo_name="x")
        called: list[int] = []
        relay.add_reconnect_listener(lambda: called.append(1))
        relay._health.record_reconnected()
        assert called == [1]


# -- O11, O13, O14: dormancy, session independence ------------------------------


class TestChannelModeOff:
    async def test_with_channel_mode_off_nothing_is_read_or_sent(
        self, rig: _Rig
    ) -> None:
        """O11."""
        off = ChannelEmitter(rig.source, rig.wakes.clear)
        off.initialized()
        await rig.mail()
        await rig.wall()
        await off.check()
        assert rig.sink.frames == []
        assert rig.source.relay.reads == 0
        assert not off.is_timer_armed()

    async def test_gates_do_not_wake_the_poller_with_channel_mode_off(
        self, rig: _Rig
    ) -> None:
        """O11."""
        woken: list[int] = []
        off = ChannelEmitter(rig.source, lambda: woken.append(1))
        off.initialized()
        off.set_mesg(enabled=False)
        off.set_mesg(enabled=True)
        off.reconnected()
        assert woken == []


class TestSessionIndependence:
    async def test_a_cleared_tool_list_session_does_not_silence_the_channel(
        self, rig: _Rig
    ) -> None:
        """O13: the sender reads no handshake session."""
        from biff.server.tools import _descriptions

        _descriptions.handshake_delivery._session = None
        rig.open()
        await rig.mail()
        await rig.emitter.check()
        assert len(rig.sink.frames) == 1


# -- content --------------------------------------------------------------------


class TestNoBodies:
    async def test_no_event_contains_a_body(self, rig: _Rig) -> None:
        rig.open()
        await rig.mail()
        await rig.wall()
        rig.invite()
        rig.source.talk.begin_connected(
            partner="eric", partner_tty="tty9", partner_key=_PEER
        )
        rig.source.talk.receive(
            {
                "type": "message",
                "from": "eric",
                "from_key": _PEER,
                "to_key": _ME,
                "body": "SECRET-MSG",
            }
        )
        await rig.emitter.check()
        assert len(rig.sink.frames) == 4
        assert all("SECRET" not in c for c in rig.sink.contents)
        assert all(len(c) <= 200 for c in rig.sink.contents)
        assert all(set(m) <= {"kind", "count"} for m in rig.sink.metas)


# -- failure logging ------------------------------------------------------------


class TestFailureLogging:
    async def test_a_stalled_client_is_one_warning_not_a_flood(
        self, rig: _Rig, caplog: pytest.LogCaptureFixture
    ) -> None:
        rig.open()
        await rig.mail()
        rig.sink.stall = True
        with caplog.at_level("WARNING", logger="biff.server.channel_emitter"):
            await rig.emitter.check()
            rig.clock.advance(2)
            await rig.emitter.deferred_send()
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "channel event (mail) failed" in warnings[0].getMessage()

    async def test_a_code_bug_in_the_relay_read_is_an_error(
        self, rig: _Rig, caplog: pytest.LogCaptureFixture
    ) -> None:
        rig.open()

        rig.source.relay.fails_with = KeyError("bug")
        with caplog.at_level("ERROR", logger="biff.server.channel_emitter"):
            await rig.emitter.check()
        assert [r.levelname for r in caplog.records] == ["ERROR"]

    async def test_a_relay_outage_is_a_warning_and_the_pass_continues(
        self, rig: _Rig, caplog: pytest.LogCaptureFixture
    ) -> None:
        rig.open()

        rig.source.relay.fails_with = TimeoutError()
        await rig.wall()
        with caplog.at_level("WARNING", logger="biff.server.channel_emitter"):
            await rig.emitter.check()
        assert [r.levelname for r in caplog.records] == ["WARNING"]
        assert rig.sink.metas == [{"kind": "wall"}]
