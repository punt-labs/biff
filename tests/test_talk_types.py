"""Tests for the pure talk value/enum types (biff.talk_types).

These types carry no behaviour beyond construction, payload parsing, and
derived properties — the ``TalkState`` machine that consumes them is
covered by ``tests/test_talk_state.py``.  Isolating the type tests here
mirrors the module split (PY-IC-9): the vocabulary is testable without the
relay-backed state machine.
"""

from __future__ import annotations

from biff.talk_types import (
    AcceptOutcome,
    AgentDrain,
    PendingInvite,
    TalkNotification,
    TalkPhase,
)

OTHER_KEY = "eric:def67890"


def _invite(from_user: str, from_key: str, body: str = "hi") -> dict[str, str]:
    return {"type": "invite", "from": from_user, "from_key": from_key, "body": body}


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

    def test_unknown_type_defaults_to_message(self) -> None:
        n = TalkNotification.from_payload({"from": "eric", "from_key": OTHER_KEY})
        assert n.ntype == "message"

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


# ---------------------------------------------------------------------------
# PendingInvite — HintNamesSession value object
# ---------------------------------------------------------------------------


class TestPendingInvite:
    def test_tty_parsed_from_session_key(self) -> None:
        inv = PendingInvite(user="eric", session_key=OTHER_KEY, arrived=0.0)
        assert inv.tty == "def67890"

    def test_accept_command_names_session(self) -> None:
        """HintNamesSession: the hint is a runnable ``talk @user:tty``."""
        inv = PendingInvite(user="eric", session_key=OTHER_KEY, arrived=0.0)
        assert inv.accept_command == "talk @eric:def67890"
        assert ":" in inv.accept_command  # never a bare @user


# ---------------------------------------------------------------------------
# AgentDrain — non-modal drain snapshot
# ---------------------------------------------------------------------------


class TestAgentDrain:
    def test_holds_drain_snapshot(self) -> None:
        note = TalkNotification.from_payload(_message("eric", OTHER_KEY, "yo"))
        invite = PendingInvite(user="priya", session_key="priya:xyz", arrived=1.0)
        drain = AgentDrain(
            messages=(note,),
            pending={"priya": invite},
            connected=True,
            ended=False,
        )
        assert drain.messages == (note,)
        assert drain.pending["priya"] is invite
        assert drain.connected is True
        assert drain.ended is False


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
