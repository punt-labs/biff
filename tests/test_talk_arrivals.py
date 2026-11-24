"""Talk arrival counters and liveness for the channel emitter (DES-087)."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from biff.relay import LocalRelay
from biff.talk_state import PENDING_INVITE_TTL, TalkState

_ME = "kai:abc12345"
_PEER = "eric:def67890"


def _state() -> TalkState:
    return TalkState(
        relay=MagicMock(spec=LocalRelay),
        user="kai",
        tty="abc12345",
        session_key=_ME,
        tty_name="tty1",
    )


def _frame(kind: str, *, key: str = _PEER, to: str = _ME) -> dict[str, str]:
    return {
        "type": kind,
        "from": "eric",
        "from_tty": "tty9",
        "from_key": key,
        "to_key": to,
        "body": "hello",
    }


def _connect(st: TalkState) -> None:
    st.begin_connected(partner="eric", partner_tty="tty9", partner_key=_PEER)


class TestInviteArrivals:
    def test_each_accepted_invite_frame_counts_once(self) -> None:
        st = _state()
        st.receive(_frame("invite"))
        st.receive(_frame("invite"))  # the same session invites again
        assert st.invite_arrivals == 2

    def test_a_drain_and_the_move_to_pending_never_lower_it(self) -> None:
        st = _state()
        st.receive(_frame("invite"))
        st.drain_for_agent()  # queued -> pending
        assert st.invite_arrivals == 1

    def test_a_frame_for_another_session_does_not_count(self) -> None:
        st = _state()
        st.receive(_frame("invite", to="kai:other"))
        assert st.invite_arrivals == 0

    def test_a_message_and_a_wake_poke_do_not_count(self) -> None:
        st = _state()
        st.receive({"from": "eric", "body": "mail"})
        st.receive(_frame("message"))
        assert st.invite_arrivals == 0


class TestLiveInvite:
    def test_a_queued_invite_is_live(self) -> None:
        st = _state()
        st.receive(_frame("invite"))
        assert st.has_live_invite()

    def test_a_pending_invite_is_live(self) -> None:
        st = _state()
        st.receive(_frame("invite"))
        st.drain_for_agent()
        assert st.has_live_invite()

    def test_no_invite_is_not_live(self) -> None:
        assert not _state().has_live_invite()

    def test_an_invite_past_the_ttl_is_not_live(self) -> None:
        st = _state()
        st.receive(_frame("invite"))
        late = time.monotonic() + PENDING_INVITE_TTL + 1
        assert not st.has_live_invite(now=late)

    def test_a_withdrawn_invite_is_not_live(self) -> None:
        st = _state()
        st.receive(_frame("invite"))
        st.receive(_frame("withdraw"))
        assert not st.has_live_invite()

    def test_the_newest_inviter_is_named(self) -> None:
        st = _state()
        st.receive(_frame("invite"))
        assert st.latest_inviter() == ("eric", "tty9")


class TestMessageArrivals:
    def test_a_partner_message_counts_once_connected(self) -> None:
        st = _state()
        _connect(st)
        st.receive(_frame("message"))
        assert st.message_arrivals == 1

    def test_a_foreign_message_does_not_count(self) -> None:
        st = _state()
        _connect(st)
        st.receive(_frame("message", key="mallory:99999999"))
        assert st.message_arrivals == 0

    def test_a_message_before_a_talk_is_accepted_does_not_count(self) -> None:
        st = _state()
        st.receive(_frame("message"))
        assert st.message_arrivals == 0

    def test_draining_does_not_lower_it(self) -> None:
        st = _state()
        _connect(st)
        st.receive(_frame("message"))
        st.drain_for_agent()
        assert st.message_arrivals == 1

    def test_a_queued_partner_message_is_live_until_drained(self) -> None:
        st = _state()
        _connect(st)
        st.receive(_frame("message"))
        assert st.has_partner_message()
        st.drain_for_agent()
        assert not st.has_partner_message()
