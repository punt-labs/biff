"""Tests for the shared talk state machine (biff.talk_state).

Coverage for the Z specification docs/talk.tex: the bounded notification
queue, session-scoped receive filters, phase-guarded drains, the accept
handshake, the mutual-invite tie-break, and ephemeral publishing.  All
tests are pure — no NATS, no network — except the publish tests, which
mock the relay at the boundary.

Partition numbers reference the TTF partition table generated from the
Z spec via ``/z-spec:partition``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from biff.nats_relay import NatsRelay
from biff.relay import LocalRelay, Relay
from biff.talk_state import (
    MAX_TALK_QUEUE,
    AcceptOutcome,
    TalkNotification,
    TalkPhase,
    TalkState,
)

MY_USER = "kai"
MY_TTY = "abc12345"
MY_KEY = "kai:abc12345"
OTHER_KEY = "eric:def67890"


def _make_state(*, relay: Relay | None = None) -> TalkState:
    """Build an idle TalkState for the ``kai`` session."""
    return TalkState(
        relay=relay if relay is not None else MagicMock(spec=LocalRelay),
        user=MY_USER,
        tty=MY_TTY,
        session_key=MY_KEY,
        tty_name="tty1",
    )


def _invite(
    from_user: str, from_key: str, body: str = "hi", to_key: str = ""
) -> dict[str, str]:
    return {
        "type": "invite",
        "from": from_user,
        "from_key": from_key,
        "body": body,
        "to_key": to_key,
    }


def _accept(from_user: str, from_key: str, to_key: str = "") -> dict[str, str]:
    return {
        "type": "accept",
        "from": from_user,
        "from_key": from_key,
        "to_key": to_key,
    }


def _message(
    from_user: str, from_key: str, body: str, tty: str = "tty2", to_key: str = ""
) -> dict[str, str]:
    return {
        "type": "message",
        "from": from_user,
        "from_tty": tty,
        "from_key": from_key,
        "body": body,
        "to_key": to_key,
    }


def _end(
    from_user: str, from_key: str, tty: str = "tty2", to_key: str = ""
) -> dict[str, str]:
    return {
        "type": "end",
        "from": from_user,
        "from_tty": tty,
        "from_key": from_key,
        "to_key": to_key,
    }


# ---------------------------------------------------------------------------
# TalkNotification
# ---------------------------------------------------------------------------


class TestTalkNotification:
    def test_from_payload_full(self) -> None:
        n = TalkNotification.from_payload(_message("eric", OTHER_KEY, "yo"))
        assert n.ntype == "message"
        assert n.nfrom == "eric"
        assert n.nfrom_tty == "tty2"
        assert n.nfrom_key == OTHER_KEY
        assert n.nbody == "yo"

    def test_unknown_type_defaults_to_message(self) -> None:
        n = TalkNotification.from_payload({"from": "eric", "from_key": OTHER_KEY})
        assert n.ntype == "message"

    def test_type_predicates(self) -> None:
        assert TalkNotification.from_payload(_invite("e", OTHER_KEY)).is_invite
        assert TalkNotification.from_payload(_accept("e", OTHER_KEY)).is_accept
        assert TalkNotification.from_payload(_end("e", OTHER_KEY)).is_end


# ---------------------------------------------------------------------------
# receive — ReceiveNotification family (talk.tex §ReceiveNotification)
# ---------------------------------------------------------------------------


class TestReceive:
    def test_enqueues_valid(self) -> None:
        st = _make_state()
        assert st.receive(_message("eric", OTHER_KEY, "hi")) is True
        assert st.queued == 1

    def test_self_echo_dropped(self) -> None:
        """ReceiveSelfEcho: a notification from our own key is dropped."""
        st = _make_state()
        assert st.receive(_message("kai", MY_KEY, "echo")) is False
        assert st.queued == 0

    def test_not_for_session_dropped(self) -> None:
        """ReceiveNotForSession: a targeted notification for another session."""
        st = _make_state()
        notif = _message("eric", OTHER_KEY, "hi", to_key="kai:other")
        assert st.receive(notif) is False
        assert st.queued == 0

    def test_broadcast_accepted(self) -> None:
        """Empty to_key is a broadcast — accepted by any session."""
        st = _make_state()
        assert st.receive(_message("eric", OTHER_KEY, "hi", to_key="")) is True

    def test_targeted_to_us_accepted(self) -> None:
        st = _make_state()
        assert st.receive(_message("eric", OTHER_KEY, "hi", to_key=MY_KEY)) is True

    def test_overflow_drops_oldest(self) -> None:
        """ReceiveOverflow: at the bound, the oldest is dropped, newest kept."""
        st = _make_state()
        for i in range(MAX_TALK_QUEUE):
            st.receive(_message("eric", OTHER_KEY, f"m{i}"))
        assert st.queued == MAX_TALK_QUEUE
        st.receive(_message("eric", OTHER_KEY, "newest"))
        assert st.queued == MAX_TALK_QUEUE
        drained = st.drain_connected()[0]
        bodies = [n.nbody for n in drained]
        assert "m0" not in bodies  # oldest evicted
        assert bodies[0] == "m1"
        assert bodies[-1] == "newest"

    def test_no_drop_at_exactly_max(self) -> None:
        st = _make_state()
        for i in range(MAX_TALK_QUEUE):
            st.receive(_message("eric", OTHER_KEY, f"m{i}"))
        assert st.queued == MAX_TALK_QUEUE


# ---------------------------------------------------------------------------
# drain_idle — DrainInvite (talk.tex §DrainInvite)
# ---------------------------------------------------------------------------


class TestDrainIdle:
    def test_records_pending_invite(self) -> None:
        st = _make_state()
        st.receive(_invite("eric", OTHER_KEY))
        drained = st.drain_idle()
        assert len(drained) == 1
        assert st.pending_invites == {"eric": OTHER_KEY}

    def test_newer_invite_supersedes(self) -> None:
        st = _make_state()
        st.receive(_invite("eric", "eric:oldsess"))
        st.drain_idle()
        st.receive(_invite("eric", OTHER_KEY))
        st.drain_idle()
        assert st.pending_invites == {"eric": OTHER_KEY}

    def test_multiple_invite_senders(self) -> None:
        st = _make_state()
        st.receive(_invite("eric", OTHER_KEY))
        st.receive(_invite("priya", "priya:xyz"))
        st.drain_idle()
        assert st.pending_invites == {"eric": OTHER_KEY, "priya": "priya:xyz"}

    def test_accept_not_recorded(self) -> None:
        st = _make_state()
        st.receive(_accept("eric", OTHER_KEY))
        st.drain_idle()
        assert st.pending_invites == {}

    def test_returns_all_drained(self) -> None:
        st = _make_state()
        st.receive(_message("eric", OTHER_KEY, "hi"))
        st.receive(_invite("eric", OTHER_KEY))
        drained = st.drain_idle()
        assert len(drained) == 2
        assert st.queued == 0


# ---------------------------------------------------------------------------
# poll_accept — handshake detection (talk.tex §DrainAcceptWhileInviting,
# §MutualAutoAccept, §DrainForeignAccept)
# ---------------------------------------------------------------------------


class TestPollAccept:
    def _inviting(self, partner_key: str) -> TalkState:
        st = _make_state()
        st.begin_invite(partner="eric", partner_tty="tty2", partner_key=partner_key)
        return st

    def test_accept_from_partner_connects(self) -> None:
        st = self._inviting(OTHER_KEY)
        st.receive(_accept("eric", OTHER_KEY))
        outcome, others = st.poll_accept()
        assert outcome is AcceptOutcome.ACCEPTED
        assert others == []
        assert st.phase is TalkPhase.CONNECTED

    def test_empty_queue_none(self) -> None:
        st = self._inviting(OTHER_KEY)
        outcome, _others = st.poll_accept()
        assert outcome is AcceptOutcome.NONE
        assert st.phase is TalkPhase.INVITING

    def test_foreign_accept_discarded(self) -> None:
        """Consent boundary: an accept from a non-invited session is ignored."""
        st = self._inviting(OTHER_KEY)
        st.receive(_accept("zed", "zed:999999"))
        outcome, _ = st.poll_accept()
        assert outcome is AcceptOutcome.NONE
        assert st.phase is TalkPhase.INVITING

    def test_third_party_invite_is_banner_not_accept(self) -> None:
        st = self._inviting(OTHER_KEY)
        st.receive(_invite("priya", "priya:xyz", body="talk?"))
        outcome, others = st.poll_accept()
        assert outcome is AcceptOutcome.NONE
        assert len(others) == 1
        assert others[0].nfrom == "priya"

    def test_mutual_invite_higher_key_auto_accepts(self) -> None:
        # MY_KEY 'kai:...' > OTHER_KEY 'eric:...' lexicographically.
        assert MY_KEY > OTHER_KEY
        st = self._inviting(OTHER_KEY)
        st.receive(_invite("eric", OTHER_KEY, body="talk?"))
        outcome, others = st.poll_accept()
        assert outcome is AcceptOutcome.AUTO_ACCEPT
        assert others == []
        assert st.phase is TalkPhase.CONNECTED

    def test_mutual_invite_lower_key_keeps_waiting(self) -> None:
        # From eric's side: OTHER_KEY < MY_KEY, so eric stays the inviter.
        st = TalkState(
            relay=MagicMock(spec=LocalRelay),
            user="eric",
            tty="def67890",
            session_key=OTHER_KEY,
        )
        st.begin_invite(partner="kai", partner_tty="tty1", partner_key=MY_KEY)
        st.receive(_invite("kai", MY_KEY, body="talk?"))
        outcome, others = st.poll_accept()
        assert outcome is AcceptOutcome.NONE
        assert others == []  # no banner spam for the partner's mutual invite
        assert st.phase is TalkPhase.INVITING

    def test_accept_beats_mutual_invite(self) -> None:
        st = self._inviting(OTHER_KEY)
        st.receive(_invite("eric", OTHER_KEY, body="talk?"))
        st.receive(_accept("eric", OTHER_KEY))
        outcome, _ = st.poll_accept()
        assert outcome is AcceptOutcome.ACCEPTED

    def test_message_not_accept(self) -> None:
        st = self._inviting(OTHER_KEY)
        st.receive(_message("eric", OTHER_KEY, "hi"))
        outcome, banners = st.poll_accept()
        assert outcome is AcceptOutcome.NONE
        # A message is not a banner — it is preserved for the connected loop.
        assert banners == []
        assert st.queued == 1

    def test_opening_message_preserved_on_accept(self) -> None:
        """The accepter's opening line survives poll_accept for the connected loop."""
        st = self._inviting(OTHER_KEY)
        st.receive(_accept("eric", OTHER_KEY))
        st.receive(_message("eric", OTHER_KEY, "opening line"))
        outcome, banners = st.poll_accept()
        assert outcome is AcceptOutcome.ACCEPTED
        assert banners == []
        surfaced, ended = st.drain_connected()
        assert [n.nbody for n in surfaced] == ["opening line"]
        assert ended is False


# ---------------------------------------------------------------------------
# drain_connected — DrainMessage / DrainEnd (talk.tex §DrainMessage, §DrainEnd)
# ---------------------------------------------------------------------------


class TestDrainConnected:
    def _connected(self) -> TalkState:
        st = _make_state()
        st.begin_connected(partner="eric", partner_tty="tty2", partner_key=OTHER_KEY)
        return st

    def test_message_surfaced(self) -> None:
        st = self._connected()
        st.receive(_message("eric", OTHER_KEY, "hello"))
        surfaced, ended = st.drain_connected()
        assert ended is False
        assert len(surfaced) == 1
        assert surfaced[0].nbody == "hello"

    def test_invite_and_accept_dropped(self) -> None:
        st = self._connected()
        st.receive(_invite("eric", OTHER_KEY))
        st.receive(_accept("eric", OTHER_KEY))
        surfaced, ended = st.drain_connected()
        assert surfaced == []
        assert ended is False

    def test_end_resets_to_idle(self) -> None:
        st = self._connected()
        st.receive(_end("eric", OTHER_KEY))
        surfaced, ended = st.drain_connected()
        assert ended is True
        assert len(surfaced) == 1
        assert surfaced[0].is_end
        assert st.phase is TalkPhase.IDLE
        assert st.partner == MY_USER  # sentinel restored

    def test_mixed_message_then_end(self) -> None:
        st = self._connected()
        st.receive(_message("eric", OTHER_KEY, "bye"))
        st.receive(_end("eric", OTHER_KEY))
        surfaced, ended = st.drain_connected()
        assert ended is True
        assert len(surfaced) == 2


# ---------------------------------------------------------------------------
# consume_pending_invite (talk.tex §RespondToInvite consumption)
# ---------------------------------------------------------------------------


class TestConsumePendingInvite:
    def _with_pending(self, mapping: dict[str, str]) -> TalkState:
        st = _make_state()
        for user, key in mapping.items():
            st.receive(_invite(user, key))
        st.drain_idle()
        return st

    def test_found_returns_key(self) -> None:
        st = self._with_pending({"eric": OTHER_KEY, "priya": "priya:xyz"})
        assert st.consume_pending_invite("eric") == OTHER_KEY

    def test_not_found_returns_none(self) -> None:
        st = self._with_pending({"priya": "priya:xyz"})
        assert st.consume_pending_invite("eric") is None

    def test_one_shot(self) -> None:
        st = self._with_pending({"eric": OTHER_KEY})
        assert st.consume_pending_invite("eric") == OTHER_KEY
        assert st.consume_pending_invite("eric") is None

    def test_empty_key_treated_as_none(self) -> None:
        st = _make_state()
        st.receive(_invite("eric", ""))
        st.drain_idle()
        assert st.consume_pending_invite("eric") is None


# ---------------------------------------------------------------------------
# Local transitions
# ---------------------------------------------------------------------------


class TestTransitions:
    def test_begin_invite_sets_inviting(self) -> None:
        st = _make_state()
        st.begin_invite(partner="eric", partner_tty="tty2", partner_key=OTHER_KEY)
        assert st.phase is TalkPhase.INVITING
        assert st.partner == "eric"
        assert st.partner_key == OTHER_KEY

    def test_begin_connected_sets_connected(self) -> None:
        st = _make_state()
        st.begin_connected(partner="eric", partner_tty="tty2", partner_key=OTHER_KEY)
        assert st.phase is TalkPhase.CONNECTED

    def test_reset_restores_idle_sentinels(self) -> None:
        st = _make_state()
        st.begin_connected(partner="eric", partner_tty="tty2", partner_key=OTHER_KEY)
        st.reset()
        assert st.phase is TalkPhase.IDLE
        assert st.partner == MY_USER
        assert st.partner_key == MY_KEY


# ---------------------------------------------------------------------------
# send_* — ephemeral publishing (talk.tex Send* side effects)
# ---------------------------------------------------------------------------


class TestSend:
    def _nats_state(self) -> tuple[TalkState, AsyncMock]:
        relay = MagicMock(spec=NatsRelay)
        nc = AsyncMock()
        relay.get_nc = AsyncMock(return_value=nc)
        relay.talk_notify_subject = MagicMock(return_value="biff.test.talk.notify.eric")
        st = _make_state(relay=relay)
        return st, nc

    @staticmethod
    def _published(nc: AsyncMock) -> tuple[str, dict[str, str]]:
        nc.publish.assert_awaited_once()
        pos = nc.publish.call_args[0]
        return str(pos[0]), json.loads(pos[1])

    @pytest.mark.anyio
    async def test_send_message(self) -> None:
        st, nc = self._nats_state()
        await st.send_message(target_user="eric", to_key=OTHER_KEY, body="hello")
        subject, payload = self._published(nc)
        assert subject == "biff.test.talk.notify.eric"
        assert payload["type"] == "message"
        assert payload["from"] == "kai"
        assert payload["from_tty"] == "tty1"
        assert payload["body"] == "hello"
        assert payload["from_key"] == MY_KEY
        assert payload["to_key"] == OTHER_KEY

    @pytest.mark.anyio
    async def test_send_message_truncates_body(self) -> None:
        st, nc = self._nats_state()
        await st.send_message(target_user="eric", to_key=OTHER_KEY, body="x" * 1000)
        _, payload = self._published(nc)
        assert len(payload["body"]) == 512

    @pytest.mark.anyio
    async def test_send_invite(self) -> None:
        st, nc = self._nats_state()
        await st.send_invite(target_user="eric", to_key=OTHER_KEY, body="wants to talk")
        _, payload = self._published(nc)
        assert payload["type"] == "invite"
        assert payload["body"] == "wants to talk"

    @pytest.mark.anyio
    async def test_send_accept(self) -> None:
        st, nc = self._nats_state()
        await st.send_accept(target_user="eric", to_key=OTHER_KEY)
        _, payload = self._published(nc)
        assert payload["type"] == "accept"
        assert payload["body"] == ""

    @pytest.mark.anyio
    async def test_send_end(self) -> None:
        st, nc = self._nats_state()
        await st.send_end(target_user="eric", to_key=OTHER_KEY)
        _, payload = self._published(nc)
        assert payload["type"] == "end"

    @pytest.mark.anyio
    async def test_send_tty_name_reflects_rename(self) -> None:
        st, nc = self._nats_state()
        st.set_tty_name("desktop")
        await st.send_message(target_user="eric", to_key=OTHER_KEY, body="hi")
        _, payload = self._published(nc)
        assert payload["from_tty"] == "desktop"

    @pytest.mark.anyio
    async def test_non_nats_relay_noop(self) -> None:
        st = _make_state(relay=MagicMock(spec=LocalRelay))
        # Should not raise and should not attempt any publish.
        await st.send_message(target_user="eric", to_key=OTHER_KEY, body="hi")
