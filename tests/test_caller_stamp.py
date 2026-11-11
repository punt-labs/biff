"""The PreToolUse stamping hook declares the caller on biff's own tools."""

from __future__ import annotations

from typing import cast

import pytest

from biff.caller_stamp import CallerStamper, main_agent_id

TOOL = "mcp__plugin_biff_tty__plan"
SID = "sess-1"


def _spec(out: dict[str, object]) -> dict[str, object]:
    return cast("dict[str, object]", out["hookSpecificOutput"])


def _updated(out: dict[str, object] | None) -> dict[str, object]:
    assert out is not None
    spec = _spec(out)
    return cast("dict[str, object]", spec["updatedInput"])


def _payload(**extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "session_id": SID,
        "tool_name": TOOL,
        "tool_input": {"message": "hi"},
    }
    return {**base, **extra}


def test_main_thread_gets_session_and_main_agent_id() -> None:
    out = CallerStamper(main_agent_id="4242").stamp(_payload())
    assert _updated(out) == {
        "message": "hi",
        "_caller": {"session_id": SID, "agent_id": "4242"},
    }


def test_subagent_payload_agent_id_wins() -> None:
    out = CallerStamper(main_agent_id="4242").stamp(_payload(agent_id="sub-7"))
    assert _updated(out)["_caller"] == {"session_id": SID, "agent_id": "sub-7"}


def test_model_supplied_caller_is_overwritten() -> None:
    forged = {"session_id": "evil", "agent_id": "evil"}
    out = CallerStamper(main_agent_id="4242").stamp(
        _payload(tool_input={"message": "hi", "_caller": forged})
    )
    assert _updated(out)["_caller"] == {"session_id": SID, "agent_id": "4242"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"session_id": None},
        {"session_id": ""},
        {"session_id": "../x"},
    ],
)
def test_indeterminate_session_removes_caller(overrides: dict[str, object]) -> None:
    forged = {"session_id": "evil", "agent_id": "evil"}
    out = CallerStamper(main_agent_id="4242").stamp(
        _payload(tool_input={"message": "hi", "_caller": forged}, **overrides)
    )
    assert _updated(out) == {"message": "hi"}


def test_indeterminate_agent_id_removes_caller() -> None:
    forged = {"session_id": "evil", "agent_id": "evil"}
    out = CallerStamper(main_agent_id=None).stamp(
        _payload(tool_input={"message": "hi", "_caller": forged})
    )
    assert _updated(out) == {"message": "hi"}


def test_never_returns_a_permission_decision() -> None:
    for stamper in (CallerStamper("1"), CallerStamper(None)):
        out = stamper.stamp(_payload())
        assert out is not None
        assert "permissionDecision" not in _spec(out)


@pytest.mark.parametrize("payload", [{}, {"session_id": SID, "tool_name": TOOL}])
def test_unreadable_payload_for_a_biff_tool_is_denied_with_cause(
    payload: dict[str, object],
) -> None:
    out = CallerStamper(main_agent_id="4242").stamp(payload)
    assert out is not None
    spec = _spec(out)
    assert spec["permissionDecision"] == "deny"
    assert "payload" in str(spec["permissionDecisionReason"])


def test_other_matched_tool_is_left_untouched() -> None:
    out = CallerStamper(main_agent_id="4242").stamp(
        _payload(tool_name="mcp__plugin_lux_lux__show")
    )
    assert out is None


@pytest.mark.parametrize(
    "name",
    [
        "mcp__plugin_biff_tty__who",
        "mcp__plugin_biff-dev_tty__who",
        "mcp__tty__who",
    ],
)
def test_every_documented_registration_name_is_stamped(name: str) -> None:
    out = CallerStamper(main_agent_id="1").stamp(_payload(tool_name=name))
    assert "_caller" in _updated(out)


def test_main_agent_id_accepts_a_live_ancestor() -> None:
    assert main_agent_id({"CLAUDE_PID": "4242"}, ancestors=frozenset({1, 4242})) == (
        "4242"
    )


@pytest.mark.parametrize(
    ("environ", "ancestors"),
    [
        ({}, frozenset({4242})),
        ({"CLAUDE_PID": "4242"}, frozenset({1})),
        ({"CLAUDE_PID": "not-a-pid"}, frozenset({4242})),
        ({"CLAUDE_PID": ""}, frozenset({4242})),
    ],
)
def test_main_agent_id_rejects_unverified_pid(
    environ: dict[str, str], ancestors: frozenset[int]
) -> None:
    assert main_agent_id(environ, ancestors=ancestors) is None
