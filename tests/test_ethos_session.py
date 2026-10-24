"""Exact participant identity and bounded child-only Ethos calls."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from biff.ethos_session import EthosSessionPin

SID = "ses_123ABC"


def roster(persona: str = "rmh") -> dict[str, object]:
    return {
        "session": SID,
        "participants": [
            {"agent_id": "jim", "persona": "jim"},
            {"agent_id": SID, "persona": persona, "parent": "jim"},
            {"agent_id": "other", "persona": "claude", "parent": "jim"},
        ],
    }


def completed(data: object, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["ethos"], returncode, json.dumps(data), "")


def test_exact_participant_and_persona_child_environment() -> None:
    pin = EthosSessionPin(SID, SID)
    before = dict(os.environ)
    with patch(
        "biff.ethos_session.subprocess.run",
        side_effect=[
            completed(roster()),
            completed({"handle": "rmh", "name": "Raymond", "kind": "agent"}),
        ],
    ) as run:
        identity = pin.resolve_identity()
    assert (identity.handle, identity.display_name, identity.kind) == (
        "rmh",
        "Raymond",
        "agent",
    )
    assert len(run.call_args_list) == 2
    assert run.call_args_list[0].args[0] == [
        "ethos",
        "session",
        "roster",
        SID,
        "--json",
    ]
    assert run.call_args_list[1].args[0] == ["ethos", "show", "rmh", "--json"]
    for call in run.call_args_list:
        assert call.kwargs["timeout"] == 2
        assert call.kwargs["env"]["ETHOS_SESSION"] == SID
        assert call.kwargs["env"]["ETHOS_AGENT_ID"] == SID
    assert dict(os.environ) == before


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"session": "ses_other", "participants": []},
        {"session": SID, "participants": []},
        {"session": SID, "participants": [{"agent_id": SID}]},
        {"session": SID, "participants": [{"agent_id": SID, "persona": "../jim"}]},
        {"session": SID, "participants": [{"agent_id": SID, "persona": "rmh"}] * 2},
    ],
)
def test_bad_roster_fails_without_second_call(data: object) -> None:
    with (
        patch("biff.ethos_session.subprocess.run", return_value=completed(data)) as run,
        pytest.raises(ValueError),
    ):
        EthosSessionPin(SID, SID).resolve_identity()
    run.assert_called_once()


@pytest.mark.parametrize(
    "identity",
    [
        {"handle": "rmh", "name": "Jim", "kind": "human"},
        {"handle": "other", "name": "Raymond", "kind": "agent"},
        {"handle": "rmh", "name": "", "kind": "agent"},
        {"handle": "rmh", "name": "   ", "kind": "agent"},
        {"handle": "rmh", "name": 123, "kind": "agent"},
        {"handle": "rmh", "name": "Raymond"},
    ],
)
def test_bad_persona_fails_closed(identity: object) -> None:
    with (
        patch(
            "biff.ethos_session.subprocess.run",
            side_effect=[completed(roster()), completed(identity)],
        ),
        pytest.raises(ValueError),
    ):
        EthosSessionPin(SID, SID).resolve_identity()


@pytest.mark.parametrize(
    "result",
    [
        completed({}, 1),
        completed([]),
        subprocess.CompletedProcess(["ethos"], 0, "not json", ""),
        FileNotFoundError(),
        subprocess.TimeoutExpired("ethos", 2),
    ],
)
def test_cli_failures_are_fatal(
    result: subprocess.CompletedProcess[str] | Exception,
) -> None:
    with patch("biff.ethos_session.subprocess.run") as run:
        if isinstance(result, Exception):
            run.side_effect = result
        else:
            run.return_value = result
        with pytest.raises(ValueError):
            EthosSessionPin(SID, SID).resolve_identity()


@pytest.mark.parametrize("sid", ["", "ses_", "../sid", "ses_bad.id", "ses_bad:id"])
def test_malformed_pin(sid: str) -> None:
    with pytest.raises(ValueError):
        EthosSessionPin(sid, sid)


def test_participant_must_equal_session() -> None:
    with pytest.raises(ValueError, match="must equal"):
        EthosSessionPin(SID, "rmh")


def test_stable_distinct_routing_and_unread(tmp_path: Path) -> None:
    first = EthosSessionPin(SID, SID)
    resumed = EthosSessionPin(SID, SID)
    other = EthosSessionPin("ses_other", "ses_other")
    assert first.routing_id == resumed.routing_id
    assert first.routing_id != other.routing_id
    assert len(first.routing_id) == 16
    assert all(c in "0123456789abcdef" for c in first.routing_id)
    assert first.unread_path(tmp_path) == resumed.unread_path(tmp_path)
    assert first.unread_path(tmp_path) != other.unread_path(tmp_path)
    assert first.unread_path(tmp_path).name == f"opencode-{first.routing_id}.json"
