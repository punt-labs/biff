"""Clients of different sessions run commands through one daemon."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from biff.client.connection import DaemonError
from biff.frame_reply import ErrorCode

if TYPE_CHECKING:
    from tests.test_daemon.conftest import Harness


def _rows(result: object) -> list[dict[str, object]]:
    data = cast("dict[str, object]", result)["data"]
    return cast("list[dict[str, object]]", data)


async def test_plan_then_who_shows_the_plan_to_another_session(
    harness: Harness,
) -> None:
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await kai.request("plan", message="ship biffd")
    rows = _rows(await eric.request("who"))
    plans = {row["user"]: row["plan"] for row in rows}
    assert plans["kai"] == "ship biffd"


async def test_write_from_one_session_is_read_by_the_other(harness: Harness) -> None:
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await kai.request("write", to="eric", message="ready for review")
    rows = _rows(await eric.request("read"))
    assert [row["body"] for row in rows] == ["ready for review"]


async def test_read_marks_messages_read(harness: Harness) -> None:
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await kai.request("write", to="eric", message="once")
    await eric.request("read")
    assert _rows(await eric.request("read")) == []


async def test_two_sessions_of_one_repo_share_one_relay_connection(
    harness: Harness,
) -> None:
    await harness.connect("s-kai", "kai")
    await harness.connect("s-eric", "eric")
    assert harness.daemon.status().relay_connections == 1
    assert len(harness.factory.built) == 1


async def test_sessions_of_different_repos_use_different_relays(
    harness: Harness,
) -> None:
    await harness.connect("s-a", "kai", cwd="/work/repo-a")
    await harness.connect("s-b", "kai", cwd="/work/repo-b")
    assert harness.daemon.status().relay_connections == 2


async def test_many_connections_for_one_target_still_open_one_relay(
    harness: Harness,
) -> None:
    for n in range(5):
        await harness.connect(f"s-{n}", "kai")
    assert len(harness.factory.built) == 1


async def test_a_session_cannot_read_another_sessions_inbox(harness: Harness) -> None:
    mine = await harness.connect("s-mine", "kai")
    theirs = await harness.connect("s-theirs", "kai")
    sender = await harness.connect("s-eric", "eric")
    address = cast("dict[str, object]", await theirs.request("status"))["data"]
    tty_name = cast("dict[str, str]", address)["tty_name"]
    await sender.request("write", to=f"kai:{tty_name}", message="for theirs only")
    assert _rows(await mine.request("read")) == []
    assert [r["body"] for r in _rows(await theirs.request("read"))] == [
        "for theirs only"
    ]


async def test_params_cannot_name_another_session(harness: Harness) -> None:
    client = await harness.connect("s-kai", "kai")
    with pytest.raises(DaemonError) as caught:
        await client.request("read", session_key="eric:abc12345")
    assert caught.value.code is ErrorCode.INVALID_PARAMS


async def test_unknown_method_is_an_error_and_the_connection_survives(
    harness: Harness,
) -> None:
    client = await harness.connect("s-kai", "kai")
    with pytest.raises(DaemonError) as caught:
        await client.request("rm_rf")
    assert caught.value.code is ErrorCode.UNKNOWN_METHOD
    assert _rows(await client.request("who"))


async def test_missing_param_is_invalid_params(harness: Harness) -> None:
    client = await harness.connect("s-kai", "kai")
    with pytest.raises(DaemonError, match="message") as caught:
        await client.request("plan")
    assert caught.value.code is ErrorCode.INVALID_PARAMS


async def test_a_command_error_is_data_not_a_protocol_error(harness: Harness) -> None:
    client = await harness.connect("s-kai", "kai")
    result = cast("dict[str, object]", await client.request("mesg", value="maybe"))
    assert result["error"] is True


async def test_results_are_wire_data_never_rendered_text(harness: Harness) -> None:
    client = await harness.connect("s-kai", "kai")
    result = cast("dict[str, object]", await client.request("who"))
    assert set(result) == {"data", "error"}


async def test_repos_on_one_relay_server_get_one_relay_each_and_stay_apart(
    harness: Harness,
) -> None:
    kai = await harness.connect("s-kai", "kai", cwd="/work/nats-a")
    eric = await harness.connect("s-eric", "eric", cwd="/work/nats-b")
    assert harness.factory.repos == ["nats-a", "nats-b"]
    await eric.request("write", to="kai", message="for repo b only")
    assert _rows(await kai.request("read")) == []
