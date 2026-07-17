"""Tests for the pure talk value/enum types (biff.talk_types).

These types carry no behaviour beyond construction, payload parsing, and
derived properties — the ``TalkState`` machine that consumes them is
covered by ``tests/test_talk_state.py``.  Isolating the type tests here
mirrors the module split (PY-IC-9): the vocabulary is testable without the
relay-backed state machine.
"""

from __future__ import annotations

import pytest

from biff.talk_types import (
    MAX_BODY_LEN,
    MAX_FIELD_LEN,
    MAX_KEY_LEN,
    AcceptOutcome,
    AgentDrain,
    PendingInvite,
    TalkNotification,
    TalkPhase,
)

OTHER_KEY = "eric:def67890"


def _invite(
    from_user: str, from_key: str, body: str = "hi", tty: str = "tty2"
) -> dict[str, str]:
    return {
        "type": "invite",
        "from": from_user,
        "from_tty": tty,
        "from_key": from_key,
        "body": body,
    }


def _accept(from_user: str, from_key: str) -> dict[str, str]:
    return {"type": "accept", "from": from_user, "from_key": from_key}


def _message(
    from_user: str, from_key: str, body: str, tty: str = "tty2"
) -> dict[str, str]:
    return {
        "type": "message",
        "from": from_user,
        "from_tty": tty,
        "from_key": from_key,
        "body": body,
    }


def _end(from_user: str, from_key: str, tty: str = "tty2") -> dict[str, str]:
    return {"type": "end", "from": from_user, "from_tty": tty, "from_key": from_key}


# ---------------------------------------------------------------------------
# TalkNotification — payload parsing and control-frame predicates
# ---------------------------------------------------------------------------


class TestTalkNotification:
    def test_from_payload_full(self) -> None:
        n = TalkNotification.from_payload(_message("eric", OTHER_KEY, "yo"))
        assert n.ntype == "message"
        assert n.nfrom == "eric"
        assert n.nfrom_tty == "tty2"
        assert n.nfrom_key == OTHER_KEY
        assert n.nbody == "yo"

    def test_missing_type_is_wake_poke(self) -> None:
        """A typeless frame (mail/wall poke) parses as a wake poke, not a message.

        The ``type`` is preserved empty rather than coerced to ``message`` so
        the poke wakes the poller without surfacing a phantom conversation line.
        """
        n = TalkNotification.from_payload({"from": "eric", "from_key": OTHER_KEY})
        assert n.ntype == ""
        assert n.is_wake_poke
        assert not n.is_control

    def test_unknown_type_is_wake_poke(self) -> None:
        """A frame typed outside the modeled set is a wake poke, never surfaced."""
        n = TalkNotification.from_payload({"type": "garbage", "from_key": OTHER_KEY})
        assert n.is_wake_poke

    def test_known_types_are_not_wake_pokes(self) -> None:
        invite = TalkNotification.from_payload(_invite("e", OTHER_KEY))
        message = TalkNotification.from_payload(_message("e", OTHER_KEY, "y"))
        assert not invite.is_wake_poke
        assert not message.is_wake_poke

    def test_missing_fields_default_to_placeholders(self) -> None:
        n = TalkNotification.from_payload({})
        assert n.nfrom == "?"
        assert n.nfrom_tty == ""
        assert n.nfrom_key == ""
        assert n.nto == ""
        assert n.nbody == ""

    def test_type_predicates(self) -> None:
        assert TalkNotification.from_payload(_invite("e", OTHER_KEY)).is_invite
        assert TalkNotification.from_payload(_accept("e", OTHER_KEY)).is_accept
        assert TalkNotification.from_payload(_end("e", OTHER_KEY)).is_end

    def test_withdraw_predicate(self) -> None:
        n = TalkNotification.from_payload({"type": "withdraw", "from_key": OTHER_KEY})
        assert n.is_withdraw
        assert not n.is_invite

    def test_sender_label_with_tty(self) -> None:
        n = TalkNotification.from_payload(_message("eric", OTHER_KEY, "yo", tty="tty2"))
        assert n.sender_label == "eric:tty2"

    def test_sender_label_without_tty_falls_back_to_user(self) -> None:
        n = TalkNotification.from_payload(_message("eric", OTHER_KEY, "yo", tty=""))
        assert n.sender_label == "eric"

    def test_sender_label_control_only_tty_collapses_to_user(self) -> None:
        # A tty that is non-empty raw but empty after neutralisation must not
        # render a dangling ``user:`` — it collapses to the bare user (biff-7g7).
        n = TalkNotification.from_payload(
            _message("eric", OTHER_KEY, "hi", tty="\x00\x1b\x07")
        )
        assert n.sender_label == "eric"

    def test_sender_label_control_only_user_falls_back_to_placeholder(self) -> None:
        # A user non-empty raw but empty after neutralisation must not render a
        # leading ``:tty`` — the user half falls back to the "?" placeholder
        # from_payload uses for a missing ``from`` (biff-7g7).
        n = TalkNotification.from_payload(_message("\x00", OTHER_KEY, "hi", tty="tty2"))
        assert n.sender_label == "?:tty2"

    def test_from_payload_clamps_oversized_wire_fields(self) -> None:
        # Every wire field is attacker-controlled (DES-046); a malicious
        # publisher can bypass the sender-side MAX_BODY_LEN truncation.  The
        # boundary must clamp so a forged megabyte field cannot be stored or
        # amplified downstream (biff-7g7).
        huge = "x" * 1_000_000
        n = TalkNotification.from_payload(
            {
                "type": huge,
                "from": huge,
                "from_tty": huge,
                "from_key": huge,
                "to_key": huge,
                "body": huge,
            }
        )
        assert len(n.nbody) <= MAX_BODY_LEN
        assert len(n.nfrom) <= MAX_FIELD_LEN
        assert len(n.nfrom_tty) <= MAX_FIELD_LEN
        assert len(n.ntype) <= MAX_FIELD_LEN
        assert len(n.nfrom_key) <= MAX_KEY_LEN
        assert len(n.nto) <= MAX_KEY_LEN

    def test_from_payload_non_str_field_uses_documented_default(self) -> None:
        # A forged payload can send JSON null (key present, value None), a number,
        # or a nested dict/list for any field.  str(None) must not leak "None" as
        # the sender, and a nested structure must not be stringified past the
        # clamp — each non-str value falls back to that field's documented default
        # (biff-7g7).
        n = TalkNotification.from_payload(
            {
                "type": "message",
                "from": None,
                "from_tty": 42,
                "from_key": OTHER_KEY,
                "body": {"nested": "structure"},
            }
        )
        assert n.nfrom == "?"  # documented missing-sender placeholder, not "None"
        assert n.nfrom_tty == ""  # number → default, not "42"
        assert n.nbody == ""  # dict → default, not a stringified mapping
        assert n.ntype == "message"  # a real str is preserved


# ---------------------------------------------------------------------------
# PendingInvite — HintNamesSession value object
# ---------------------------------------------------------------------------


class TestPendingInvite:
    def test_accept_command_uses_display_tty(self) -> None:
        """The hint names the session by the display tty ``/who`` shows.

        Not the opaque session-key hex — ``talk @eric:tty2`` is what the
        recipient types and what ``resolve_talk_target`` matches.
        """
        inv = PendingInvite(user="eric", session_key=OTHER_KEY, tty="tty2", arrived=0.0)
        assert inv.accept_command == "talk @eric:tty2"
        assert ":" in inv.accept_command  # never a bare @user

    def test_accept_command_falls_back_to_key_without_tty(self) -> None:
        """A frame with no display tty still renders a runnable session hint."""
        inv = PendingInvite(user="eric", session_key=OTHER_KEY, tty="", arrived=0.0)
        assert inv.accept_command == "talk @eric:def67890"

    def test_colonless_key_rejected(self) -> None:
        """HintNamesSession: a key with no ``:`` cannot name a session."""
        with pytest.raises(ValueError, match="user:tty"):
            PendingInvite(user="eric", session_key="eric", tty="tty2", arrived=0.0)

    def test_empty_tty_key_rejected(self) -> None:
        """HintNamesSession: a ``user:`` key with an empty tty is malformed."""
        with pytest.raises(ValueError, match="user:tty"):
            PendingInvite(user="eric", session_key="eric:", tty="tty2", arrived=0.0)

    def test_empty_user_key_rejected(self) -> None:
        """HintNamesSession: a ``:tty`` key with an empty user is malformed.

        It would render ``talk @:def67890``, which names no user and fails at
        the prompt, so it must be rejected at construction like the other halves.
        """
        with pytest.raises(ValueError, match="user:tty"):
            PendingInvite(user="eric", session_key=":def67890", tty="tty2", arrived=0.0)

    def test_well_formed_key_accepted(self) -> None:
        inv = PendingInvite(user="eric", session_key=OTHER_KEY, tty="tty2", arrived=0.0)
        assert inv.session_key == OTHER_KEY

    def test_user_mismatched_key_rejected(self) -> None:
        """A frame whose ``from`` and ``from_key`` name different users is forged.

        The accept path derives its target from ``session_key`` while the
        pending set is keyed by ``user``; a mismatch would let an accept
        addressed to ``eric`` connect to ``mallory`` instead, so it is dropped
        at construction.
        """
        with pytest.raises(ValueError, match="does not match session-key user"):
            PendingInvite(
                user="eric", session_key="mallory:def67890", tty="tty2", arrived=0.0
            )

    def test_from_notification_rejects_user_mismatch(self) -> None:
        """The wire boundary drops an invite whose ``from`` and key user differ."""
        notif = TalkNotification.from_payload(
            {"type": "invite", "from": "eric", "from_key": "mallory:sess"}
        )
        with pytest.raises(ValueError, match="does not match session-key user"):
            PendingInvite.from_notification(notif)

    def test_from_notification_carries_display_tty(self) -> None:
        notif = TalkNotification.from_payload(_invite("eric", OTHER_KEY, tty="tty2"))
        inv = PendingInvite.from_notification(notif)
        assert inv.user == "eric"
        assert inv.session_key == OTHER_KEY
        assert inv.tty == "tty2"
        assert inv.accept_command == "talk @eric:tty2"

    def test_from_notification_rejects_keyless_frame(self) -> None:
        """A keyless invite frame is rejected at the wire boundary, not recorded."""
        notif = TalkNotification.from_payload(_invite("eric", ""))
        with pytest.raises(ValueError, match="user:tty"):
            PendingInvite.from_notification(notif)


# ---------------------------------------------------------------------------
# AgentDrain — non-modal drain snapshot
# ---------------------------------------------------------------------------


class TestAgentDrain:
    def test_holds_drain_snapshot(self) -> None:
        note = TalkNotification.from_payload(_message("eric", OTHER_KEY, "yo"))
        invite = PendingInvite(
            user="priya", session_key="priya:xyz", tty="tty3", arrived=1.0
        )
        drain = AgentDrain(messages=(note,), pending={"priya": invite})
        assert drain.messages == (note,)
        assert drain.pending["priya"] is invite


# ---------------------------------------------------------------------------
# Enums — distinct phases and accept outcomes
# ---------------------------------------------------------------------------


class TestEnums:
    def test_phases_are_distinct(self) -> None:
        assert len({TalkPhase.IDLE, TalkPhase.INVITING, TalkPhase.CONNECTED}) == 3

    def test_accept_outcomes_are_distinct(self) -> None:
        outcomes = {
            AcceptOutcome.NONE,
            AcceptOutcome.ACCEPTED,
            AcceptOutcome.AUTO_ACCEPT,
        }
        assert len(outcomes) == 3
