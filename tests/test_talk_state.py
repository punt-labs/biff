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
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from biff.nats_relay import NatsRelay
from biff.relay import LocalRelay, Relay
from biff.talk_state import (
    MAX_PENDING_INVITES,
    MAX_TALK_QUEUE,
    PENDING_INVITE_TTL,
    TalkState,
)
from biff.talk_types import AcceptOutcome, TalkPhase

MY_USER = "kai"
MY_TTY = "abc12345"
MY_KEY = "kai:abc12345"
OTHER_KEY = "eric:def67890"


def _pending_keys(st: TalkState) -> dict[str, str]:
    """The user-to-session-key view of the held pending invites."""
    return {user: inv.session_key for user, inv in st.pending_invites.items()}


def _withdraw(from_user: str, from_key: str, to_key: str = MY_KEY) -> dict[str, str]:
    return {
        "type": "withdraw",
        "from": from_user,
        "from_key": from_key,
        "to_key": to_key,
    }


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
    from_user: str, from_key: str, body: str = "hi", to_key: str = MY_KEY
) -> dict[str, str]:
    return {
        "type": "invite",
        "from": from_user,
        "from_key": from_key,
        "body": body,
        "to_key": to_key,
    }


def _accept(from_user: str, from_key: str, to_key: str = MY_KEY) -> dict[str, str]:
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
    from_user: str, from_key: str, tty: str = "tty2", to_key: str = MY_KEY
) -> dict[str, str]:
    return {
        "type": "end",
        "from": from_user,
        "from_tty": tty,
        "from_key": from_key,
        "to_key": to_key,
    }


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

    def test_broadcast_message_accepted(self) -> None:
        """A keyless *message* is a broadcast poke (write/wall mail) — accepted."""
        st = _make_state()
        assert st.receive(_message("eric", OTHER_KEY, "hi", to_key="")) is True

    def test_keyless_invite_dropped(self) -> None:
        """A keyless *control* frame is dropped (ReceiveNotForSession)."""
        st = _make_state()
        assert st.receive(_invite("eric", OTHER_KEY, to_key="")) is False
        assert st.queued == 0

    def test_keyless_withdraw_dropped(self) -> None:
        """A keyless withdraw cannot apply to all sessions — dropped, invite intact."""
        st = _make_state()
        st.receive(_invite("eric", OTHER_KEY))
        st.drain_idle()
        assert st.receive(_withdraw("eric", OTHER_KEY, to_key="")) is False
        assert _pending_keys(st) == {"eric": OTHER_KEY}

    def test_targeted_to_us_accepted(self) -> None:
        st = _make_state()
        assert st.receive(_message("eric", OTHER_KEY, "hi", to_key=MY_KEY)) is True

    def test_targeted_control_frame_accepted(self) -> None:
        """A control frame naming our session is accepted (ReceiveNotForSession).

        The positive mirror of ``test_keyless_invite_dropped``: the ``nto ==
        myKey`` guard admits a control frame addressed to us, not just messages.
        """
        st = _make_state()
        assert st.receive(_invite("eric", OTHER_KEY, to_key=MY_KEY)) is True
        assert st.queued == 1

    def test_overflow_drops_oldest(self) -> None:
        """ReceiveOverflow: at the bound, the oldest is dropped, newest kept."""
        st = _make_state()
        # Connected to the sender so drain_connected surfaces the partner's
        # messages (DrainForeignMessage binds surfacing to the partner key).
        st.begin_connected(partner="eric", partner_tty="tty2", partner_key=OTHER_KEY)
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
        assert _pending_keys(st) == {"eric": OTHER_KEY}

    def test_newer_invite_supersedes(self) -> None:
        st = _make_state()
        st.receive(_invite("eric", "eric:oldsess"))
        st.drain_idle()
        st.receive(_invite("eric", OTHER_KEY))
        st.drain_idle()
        assert _pending_keys(st) == {"eric": OTHER_KEY}

    def test_multiple_invite_senders(self) -> None:
        st = _make_state()
        st.receive(_invite("eric", OTHER_KEY))
        st.receive(_invite("priya", "priya:xyz"))
        st.drain_idle()
        assert _pending_keys(st) == {"eric": OTHER_KEY, "priya": "priya:xyz"}

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
# Connected-phase partner binding (talk.tex DrainForeignMessage / DrainForeignEnd)
# ---------------------------------------------------------------------------


_FORGED_KEY = "mallory:99999999"


class TestConnectedPartnerBinding:
    """While connected, only the partner's frames surface or hang up.

    A forged message or end from a key other than ``partner_key`` is dequeued
    and skipped — it cannot inject a line or tear down the conversation.
    """

    def _connected(self) -> TalkState:
        st = _make_state()
        st.begin_connected(partner="eric", partner_tty="tty2", partner_key=OTHER_KEY)
        return st

    def test_drain_connected_drops_foreign_message(self) -> None:
        st = self._connected()
        st.receive(_message("mallory", _FORGED_KEY, "forged", to_key=MY_KEY))
        surfaced, ended = st.drain_connected()
        assert surfaced == []
        assert ended is False

    def test_drain_connected_ignores_foreign_end(self) -> None:
        st = self._connected()
        st.receive(_end("mallory", _FORGED_KEY))
        _surfaced, ended = st.drain_connected()
        assert ended is False
        assert st.phase is TalkPhase.CONNECTED  # forged end did not hang us up

    def test_drain_connected_surfaces_partner_message(self) -> None:
        st = self._connected()
        st.receive(_message("eric", OTHER_KEY, "real", to_key=MY_KEY))
        surfaced, _ended = st.drain_connected()
        assert [n.nbody for n in surfaced] == ["real"]

    def test_drain_connected_partner_end_resets(self) -> None:
        st = self._connected()
        st.receive(_end("eric", OTHER_KEY))
        _surfaced, ended = st.drain_connected()
        assert ended is True
        assert st.phase is TalkPhase.IDLE

    def test_drain_for_agent_drops_foreign_message_while_connected(self) -> None:
        st = self._connected()
        st.receive(_message("mallory", _FORGED_KEY, "forged", to_key=MY_KEY))
        drain = st.drain_for_agent()
        assert drain.messages == ()

    def test_drain_for_agent_ignores_foreign_end_while_connected(self) -> None:
        st = self._connected()
        st.receive(_end("mallory", _FORGED_KEY))
        drain = st.drain_for_agent()
        assert st.phase is TalkPhase.CONNECTED
        assert not any(n.is_end for n in drain.messages)

    def test_drain_for_agent_surfaces_partner_message_while_connected(self) -> None:
        st = self._connected()
        st.receive(_message("eric", OTHER_KEY, "real", to_key=MY_KEY))
        drain = st.drain_for_agent()
        assert [n.nbody for n in drain.messages] == ["real"]

    def test_drain_for_agent_partner_end_resets_while_connected(self) -> None:
        st = self._connected()
        st.receive(_end("eric", OTHER_KEY))
        drain = st.drain_for_agent()
        assert any(n.is_end for n in drain.messages)
        assert st.phase is TalkPhase.IDLE


# ---------------------------------------------------------------------------
# Inviting-phase end binding (talk.tex DrainEnd guards phase = tpConnected)
# ---------------------------------------------------------------------------


class TestInvitingPhaseEnd:
    """An ``end`` frame resets only while connected — never while inviting.

    ``talk.tex`` guards both ``DrainEnd`` and ``DrainForeignEnd`` on
    ``phase = tpConnected``: no schema resets on an end while inviting.  A
    forged ``end`` must not cancel an outstanding outgoing invite.
    """

    def _inviting(self, *, partner_key: str = OTHER_KEY) -> TalkState:
        st = _make_state()
        st.begin_invite(partner="eric", partner_tty="tty2", partner_key=partner_key)
        return st

    def test_foreign_end_while_inviting_does_not_reset(self) -> None:
        st = self._inviting()
        st.receive(_end("mallory", _FORGED_KEY))
        drain = st.drain_for_agent()
        assert st.phase is TalkPhase.INVITING  # forged end did not cancel the invite
        assert not any(n.is_end for n in drain.messages)

    def test_partner_keyed_end_while_inviting_does_not_reset(self) -> None:
        """Even the invited session's end cannot reset an unaccepted invite.

        There is no live connection to hang up before the accept, and
        ``DrainEnd`` guards ``phase = tpConnected`` — so the end is dropped.
        """
        st = self._inviting()
        st.receive(_end("eric", OTHER_KEY))
        drain = st.drain_for_agent()
        assert st.phase is TalkPhase.INVITING
        assert not any(n.is_end for n in drain.messages)

    def test_accept_still_connects_while_inviting(self) -> None:
        """The invited partner's accept still completes the handshake."""
        st = self._inviting()
        st.receive(_accept("eric", OTHER_KEY))
        st.drain_for_agent()
        assert st.phase is TalkPhase.CONNECTED

    def test_end_while_idle_does_not_reset_or_surface(self) -> None:
        st = _make_state()
        st.receive(_end("mallory", _FORGED_KEY))
        drain = st.drain_for_agent()
        assert st.phase is TalkPhase.IDLE
        assert not any(n.is_end for n in drain.messages)


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

    def test_found_returns_invite(self) -> None:
        st = self._with_pending({"eric": OTHER_KEY, "priya": "priya:xyz"})
        invite = st.consume_pending_invite("eric")
        assert invite is not None
        assert invite.session_key == OTHER_KEY

    def test_not_found_returns_none(self) -> None:
        st = self._with_pending({"priya": "priya:xyz"})
        assert st.consume_pending_invite("eric") is None

    def test_one_shot(self) -> None:
        st = self._with_pending({"eric": OTHER_KEY})
        first = st.consume_pending_invite("eric")
        assert first is not None
        assert first.session_key == OTHER_KEY
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

    @pytest.mark.anyio
    async def test_send_withdraw(self) -> None:
        st, nc = self._nats_state()
        await st.send_withdraw(target_user="eric", to_key=OTHER_KEY)
        _, payload = self._published(nc)
        assert payload["type"] == "withdraw"
        assert payload["to_key"] == OTHER_KEY


# ---------------------------------------------------------------------------
# HintNamesSession: keyless invites are never recorded (talkBare stays empty)
# ---------------------------------------------------------------------------


class TestHintNamesSession:
    def test_keyless_invite_not_recorded(self) -> None:
        """A keyless invite cannot name a session, so it never becomes pending."""
        st = _make_state()
        st.receive(_invite("eric", ""))
        st.drain_idle()
        assert st.pending_invites == {}

    def test_recorded_invite_always_carries_a_named_session(self) -> None:
        st = _make_state()
        st.receive(_invite("eric", OTHER_KEY))
        st.receive(_invite("priya", "priya:xyz"))
        st.drain_for_agent()
        for invite in st.pending_invites.values():
            assert ":" in invite.session_key
            assert invite.accept_command.startswith("talk @")
            assert invite.accept_command != f"talk @{invite.user}"


# ---------------------------------------------------------------------------
# WithdrawArrive — ntWithdraw frame (notification.tex §WithdrawArrive)
# ---------------------------------------------------------------------------


class TestWithdraw:
    def test_withdraw_removes_drained_pending(self) -> None:
        """P-WD-1: a withdraw drops an invite already drained into pending."""
        st = _make_state()
        st.receive(_invite("eric", OTHER_KEY))
        st.drain_idle()
        assert _pending_keys(st) == {"eric": OTHER_KEY}
        assert st.receive(_withdraw("eric", OTHER_KEY)) is True
        assert st.pending_invites == {}

    def test_withdraw_cancels_undrained_queued_invite(self) -> None:
        """A withdraw cancels an invite still queued (not yet drained)."""
        st = _make_state()
        st.receive(_invite("eric", OTHER_KEY))
        assert st.queued == 1
        assert st.receive(_withdraw("eric", OTHER_KEY)) is True
        assert st.queued == 0
        st.drain_idle()
        assert st.pending_invites == {}

    def test_withdraw_one_of_several_preserves_others(self) -> None:
        """P-WD-2: withdrawing one inviter leaves the rest pending."""
        st = _make_state()
        st.receive(_invite("eric", OTHER_KEY))
        st.receive(_invite("priya", "priya:xyz"))
        st.drain_idle()
        st.receive(_withdraw("eric", OTHER_KEY))
        assert _pending_keys(st) == {"priya": "priya:xyz"}

    def test_withdraw_non_pending_is_noop(self) -> None:
        """P-WD-neg: withdrawing an unknown inviter changes nothing."""
        st = _make_state()
        st.receive(_invite("eric", OTHER_KEY))
        st.drain_idle()
        assert st.receive(_withdraw("zed", "zed:999")) is False
        assert _pending_keys(st) == {"eric": OTHER_KEY}

    def test_key_matched_withdraw_clears(self) -> None:
        """WithdrawArrive: a withdraw whose key matches the invite clears it."""
        st = _make_state()
        st.receive(_invite("eric", OTHER_KEY))
        st.drain_idle()
        assert st.receive(_withdraw("eric", OTHER_KEY)) is True
        assert st.pending_invites == {}

    def test_stale_cross_session_withdraw_preserves_live_invite(self) -> None:
        """Reordering: a late session-A withdraw keeps a live session-B invite.

        Core NATS gives no cross-session ordering, so an ``ntWithdraw`` from an
        earlier session can arrive after a fresh invite from a later session of
        the same user.  Keyed on the user alone it would wrongly clear the live
        invite (WithdrawStale); the key guard drops it (WithdrawForeign).
        """
        st = _make_state()
        st.receive(_invite("eric", "eric:sessionB"))  # the live invite
        st.drain_idle()
        assert _pending_keys(st) == {"eric": "eric:sessionB"}
        # A stale withdraw keyed to eric's earlier session A arrives late.
        assert st.receive(_withdraw("eric", "eric:sessionA")) is False
        assert _pending_keys(st) == {"eric": "eric:sessionB"}

    def test_foreign_key_withdraw_is_noop(self) -> None:
        """WithdrawForeign: a withdraw whose key names another session is dropped."""
        st = _make_state()
        st.receive(_invite("eric", OTHER_KEY))
        st.drain_idle()
        assert st.receive(_withdraw("eric", "eric:wrongkey")) is False
        assert _pending_keys(st) == {"eric": OTHER_KEY}

    def test_foreign_key_withdraw_preserves_undrained_queued_invite(self) -> None:
        """WithdrawForeign on the queue branch: a foreign-key withdraw against a
        still-queued invite leaves it intact (the ``n.nfrom_key == withdraw_key``
        queue guard).

        The other foreign-key tests drain first, exercising only the ``_pending``
        branch; this covers the undrained queue path of the same key guard.
        """
        st = _make_state()
        st.receive(_invite("eric", "eric:sessionB"))  # live invite, still queued
        assert st.queued == 1
        # A withdraw keyed to eric's earlier session A arrives before we drain.
        assert st.receive(_withdraw("eric", "eric:sessionA")) is False
        assert st.queued == 1  # the live queued invite is preserved
        st.drain_idle()
        assert _pending_keys(st) == {"eric": "eric:sessionB"}


# ---------------------------------------------------------------------------
# has_activity — the talk_listen wait predicate
# ---------------------------------------------------------------------------


class TestHasActivity:
    """``talk_listen`` waits on ``has_activity`` so a drained invite returns."""

    def test_idle_empty_has_no_activity(self) -> None:
        assert _make_state().has_activity is False

    def test_queued_frame_is_activity(self) -> None:
        st = _make_state()
        st.receive(_message("eric", OTHER_KEY, "hi"))
        assert st.has_activity is True

    def test_pending_invite_is_activity_with_empty_queue(self) -> None:
        """A drained invite (queue empty) is activity — not silence to sleep on."""
        st = _make_state()
        st.receive(_invite("eric", OTHER_KEY))
        st.drain_idle()  # invite moves to pending; queue drains to empty
        assert st.queued == 0
        assert st.has_activity is True

    def test_connected_is_activity(self) -> None:
        st = _make_state()
        st.begin_connected(partner="eric", partner_tty="tty2", partner_key=OTHER_KEY)
        assert st.has_activity is True


# ---------------------------------------------------------------------------
# ExpirePendingInvite / AgeTick — TTL sweep (notification.tex §ExpirePendingInvite)
# ---------------------------------------------------------------------------


class TestExpiry:
    def _pending_state(self) -> tuple[TalkState, float]:
        st = _make_state()
        st.receive(_invite("eric", OTHER_KEY))
        st.drain_idle()
        return st, st.pending_invites["eric"].arrived

    def test_fresh_invite_survives_a_tick(self) -> None:
        """P-EXP-boundary: an invite below the TTL is never reaped."""
        st, arrived = self._pending_state()
        assert st.expire_stale_invites(now=arrived + PENDING_INVITE_TTL - 0.001) == 0
        assert _pending_keys(st) == {"eric": OTHER_KEY}

    def test_matured_invite_reaped_at_ttl(self) -> None:
        """P-EXP-1: an invite whose age reaches the TTL is reaped."""
        st, arrived = self._pending_state()
        assert st.expire_stale_invites(now=arrived + PENDING_INVITE_TTL) == 1
        assert st.pending_invites == {}

    def test_expiry_preserves_a_fresh_sibling(self) -> None:
        """P-EXP-2: only invites past the TTL are removed, not fresh ones."""
        st, arrived = self._pending_state()
        # A second, fresher invite recorded well after the first.
        st.receive(_invite("priya", "priya:xyz"))
        st.drain_idle()
        priya_arrived = st.pending_invites["priya"].arrived
        # Sweep at a time that has matured eric but not priya.
        now = arrived + PENDING_INVITE_TTL
        assert now - priya_arrived < PENDING_INVITE_TTL
        assert st.expire_stale_invites(now=now) == 1
        assert _pending_keys(st) == {"priya": "priya:xyz"}

    def test_undrained_queued_invite_reaped_at_ttl(self) -> None:
        """P-EXP-backstop: an invite never drained into ``_pending`` still ages
        out of the queue, so a crashed inviter cannot strand the marker.
        """
        st = _make_state()
        before = time.monotonic()
        st.receive(_invite("eric", OTHER_KEY))  # enqueued, never drained
        assert st.queued == 1  # the queued frame lights the marker
        # Below the bound: the queued invite survives.
        assert st.expire_stale_invites(now=before + PENDING_INVITE_TTL - 1.0) == 0
        assert st.queued == 1
        # Past the bound: the queued invite is dropped and the marker clears.
        after = time.monotonic()
        assert st.expire_stale_invites(now=after + PENDING_INVITE_TTL) == 1
        assert st.queued == 0
        assert st.pending_invites == {}

    def test_undrained_queued_message_survives_ttl(self) -> None:
        """A queued message has no time-to-live — only invites age out — so a
        stuck message is left for the agent's drain, never silently reaped.
        """
        st = _make_state()
        st.receive(_message("eric", OTHER_KEY, "still here"))
        after = time.monotonic()
        assert st.expire_stale_invites(now=after + PENDING_INVITE_TTL) == 0
        assert st.queued == 1

    def test_drain_preserves_enqueue_ttl_anchor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The TTL is anchored to enqueue time, not drain time.

        An invite enqueued at T and drained at T+X must still be reaped at
        T+TTL — draining it into ``_pending`` must not restart the clock, or a
        late drain would let the invite outlive the window (up to ~2x).
        """
        monkeypatch.setattr("biff.talk_state.time.monotonic", lambda: 1000.0)
        st = _make_state()
        st.receive(_invite("eric", OTHER_KEY))  # enqueued at T=1000
        monkeypatch.setattr("biff.talk_state.time.monotonic", lambda: 1200.0)
        st.drain_idle()  # drained 200s later; arrived must stay 1000, not 1200
        assert st.pending_invites["eric"].arrived == 1000.0
        # Not yet reaped just before the original window closes.
        assert st.expire_stale_invites(now=1000.0 + PENDING_INVITE_TTL - 0.001) == 0
        # Reaped exactly at enqueue + TTL — the drain did not restart the clock.
        assert st.expire_stale_invites(now=1000.0 + PENDING_INVITE_TTL) == 1


# ---------------------------------------------------------------------------
# Grow-only regression guards (notification.tex TalkDrain / TalkAccept)
# ---------------------------------------------------------------------------


class TestGrowOnlyGuards:
    def test_drain_does_not_readd_consumed_invite(self) -> None:
        """A consumed invite is not resurrected by a later empty drain."""
        st = _make_state()
        st.receive(_invite("eric", OTHER_KEY))
        st.drain_for_agent()
        consumed = st.consume_pending_invite("eric")
        assert consumed is not None
        assert consumed.session_key == OTHER_KEY
        st.drain_for_agent()  # empty queue — must not re-add
        assert st.pending_invites == {}

    def test_auto_accept_consumes_pending_not_stranded(self) -> None:
        """TalkAccept: a mutual auto-accept connects and clears the invite."""
        st = _make_state()
        st.begin_invite(partner="eric", partner_tty="tty2", partner_key=OTHER_KEY)
        st.receive(_invite("eric", OTHER_KEY, body="talk?"))
        st.drain_for_agent()
        assert st.phase is TalkPhase.CONNECTED
        assert st.pending_invites == {}  # moved to connected, not stranded

    def test_accept_consumes_partner_pending(self) -> None:
        """An accept from the invited session leaves no stranded pending entry."""
        st = _make_state()
        st.begin_invite(partner="eric", partner_tty="tty2", partner_key=OTHER_KEY)
        st.receive(_invite("eric", OTHER_KEY))  # eric also invited us
        st.receive(_accept("eric", OTHER_KEY))  # then accepted ours
        st.drain_for_agent()
        assert st.phase is TalkPhase.CONNECTED
        assert st.pending_invites == {}


# ---------------------------------------------------------------------------
# Pending-set bound — drop-oldest on new keys (notification.tex maxPending)
# ---------------------------------------------------------------------------


class TestPendingBound:
    """``_pending`` is bounded at ``MAX_PENDING_INVITES`` (drop-oldest)."""

    @staticmethod
    def _fill_to_cap(st: TalkState) -> None:
        for i in range(MAX_PENDING_INVITES):
            st.receive(_invite(f"u{i}", f"u{i}:s{i}"))
        st.drain_idle()

    def test_new_inviter_at_cap_evicts_oldest(self) -> None:
        st = _make_state()
        self._fill_to_cap(st)
        assert len(st.pending_invites) == MAX_PENDING_INVITES
        st.receive(_invite("newcomer", "newcomer:sess"))
        st.drain_idle()
        assert len(st.pending_invites) == MAX_PENDING_INVITES  # still bounded
        assert "u0" not in st.pending_invites  # oldest-by-arrival evicted
        assert "newcomer" in st.pending_invites

    def test_supersede_at_cap_does_not_evict(self) -> None:
        st = _make_state()
        self._fill_to_cap(st)
        st.receive(_invite("u0", "u0:newsession"))  # same inviter, new session
        st.drain_idle()
        assert len(st.pending_invites) == MAX_PENDING_INVITES  # nothing evicted
        assert st.pending_invites["u0"].session_key == "u0:newsession"

    def test_bound_never_exceeded(self) -> None:
        st = _make_state()
        for i in range(MAX_PENDING_INVITES + 50):
            st.receive(_invite(f"u{i}", f"u{i}:s{i}"))
            st.drain_idle()  # drain each so the queue bound never masks this
            assert len(st.pending_invites) <= MAX_PENDING_INVITES
        assert len(st.pending_invites) == MAX_PENDING_INVITES
