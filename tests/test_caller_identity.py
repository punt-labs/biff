"""CallerIdentity validates the harness-declared {session_id, agent_id} pair."""

from __future__ import annotations

import pytest

from biff.caller_identity import CallerIdentity, CallerIdentityError
from biff.session_id import CLAUDE_CODE_ROLE, derive_routing_id


def test_valid_wire_value_parses() -> None:
    caller = CallerIdentity.from_wire({"session_id": "abc-123_X", "agent_id": "a1"})
    assert caller == CallerIdentity(session_id="abc-123_X", agent_id="a1")


def test_routing_id_derives_from_session_id_and_role() -> None:
    caller = CallerIdentity(session_id="abc-123", agent_id="a1")
    assert caller.routing_id == derive_routing_id("abc-123", CLAUDE_CODE_ROLE)


def test_role_is_the_claude_code_constant() -> None:
    assert CLAUDE_CODE_ROLE == "biff.claude-code.session.v1"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "abc",
        [],
        {},
        {"session_id": "s"},
        {"agent_id": "a"},
        {"session_id": "", "agent_id": "a"},
        {"session_id": "s", "agent_id": ""},
        {"session_id": 1, "agent_id": "a"},
        {"session_id": "s", "agent_id": None},
        {"session_id": "../etc", "agent_id": "a"},
        {"session_id": "s", "agent_id": "a b"},
        {"session_id": "s\n", "agent_id": "a"},
    ],
)
def test_malformed_wire_value_is_named_error(raw: object) -> None:
    with pytest.raises(CallerIdentityError, match="_caller"):
        CallerIdentity.from_wire(raw)
