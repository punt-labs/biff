"""Session records: attach, detach, a new run, conflicts, presence."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import pytest

from biff.client.connection import ConnectionLostError, DaemonError
from biff.frame_reply import ErrorCode
from biff.relay import LocalRelay
from biff.session_id import CLI_ROLE
from biff.session_routing import SessionRouting

if TYPE_CHECKING:
    from tests.test_daemon.conftest import Harness


def _attached(harness: Harness, session_id: str) -> int:
    (session,) = [
        s for s in harness.daemon.status().sessions if s.session_id == session_id
    ]
    return session.attached


async def test_a_second_client_for_one_session_id_shares_the_record(
    harness: Harness,
) -> None:
    await harness.connect("s-kai", "kai")
    await harness.connect("s-kai", "kai", client="hook")
    assert (len(harness.daemon.status().sessions), _attached(harness, "s-kai")) == (
        1,
        2,
    )


async def test_two_clients_of_one_session_see_one_plan(harness: Harness) -> None:
    first = await harness.connect("s-kai", "kai")
    second = await harness.connect("s-kai", "kai", client="hook")
    await first.request("plan", message="one session")
    data = cast("dict[str, object]", await second.request("status"))["data"]
    assert cast("dict[str, str]", data)["session_key"].startswith("kai:")


async def test_dropping_a_connection_detaches_without_ending_the_session(
    harness: Harness,
) -> None:
    client = await harness.connect("s-kai", "kai")
    await client.close()
    await asyncio.sleep(0.05)
    assert _attached(harness, "s-kai") == 0


async def test_a_detached_session_keeps_its_presence_row(harness: Harness) -> None:
    client = await harness.connect("s-kai", "kai")
    await client.close()
    await asyncio.sleep(0.05)
    relay = LocalRelay(harness.factory.built[0])
    key = SessionRouting("s-kai", role=CLI_ROLE).session_key("kai")
    assert await relay.get_session(key) is not None


async def test_a_session_reattaches_after_detaching(harness: Harness) -> None:
    first = await harness.connect("s-kai", "kai")
    await first.close()
    await asyncio.sleep(0.05)
    again = await harness.connect("s-kai", "kai")
    assert cast("dict[str, object]", await again.request("who"))["error"] is False


async def test_a_new_main_agent_id_ends_the_replaced_runs_connection(
    harness: Harness,
) -> None:
    old = await harness.connect("s-kai", "kai", main_agent_id="run-1")
    await harness.connect("s-kai", "kai", main_agent_id="run-2")
    with pytest.raises((DaemonError, ConnectionLostError)):
        for _ in range(50):
            await old.request("who")
            await asyncio.sleep(0.02)


async def test_the_new_run_keeps_working(harness: Harness) -> None:
    await harness.connect("s-kai", "kai", main_agent_id="run-1")
    new = await harness.connect("s-kai", "kai", main_agent_id="run-2")
    assert cast("dict[str, object]", await new.request("who"))["error"] is False


async def test_a_new_run_does_not_touch_another_session(harness: Harness) -> None:
    bystander = await harness.connect("s-eric", "eric", main_agent_id="run-9")
    await harness.connect("s-kai", "kai", main_agent_id="run-1")
    await harness.connect("s-kai", "kai", main_agent_id="run-2")
    assert cast("dict[str, object]", await bystander.request("who"))["error"] is False


async def test_a_client_that_states_no_run_is_never_replaced(
    harness: Harness,
) -> None:
    shell = await harness.connect("s-kai", "kai", main_agent_id="")
    await harness.connect("s-kai", "kai", main_agent_id="run-1")
    await harness.connect("s-kai", "kai", main_agent_id="run-2")
    assert cast("dict[str, object]", await shell.request("who"))["error"] is False


async def test_a_session_id_cannot_be_claimed_by_another_user(
    harness: Harness,
) -> None:
    await harness.connect("s-kai", "kai")
    with pytest.raises(DaemonError) as caught:
        await harness.connect("s-kai", "eric")
    assert caught.value.code is ErrorCode.SESSION_CONFLICT


async def test_a_session_id_cannot_move_to_another_relay_target(
    harness: Harness,
) -> None:
    await harness.connect("s-kai", "kai", cwd="/work/repo-a")
    with pytest.raises(DaemonError) as caught:
        await harness.connect("s-kai", "kai", cwd="/work/repo-b")
    assert caught.value.code is ErrorCode.SESSION_CONFLICT


async def test_the_daemon_heartbeats_each_attached_session_once(
    harness: Harness,
) -> None:
    await harness.connect("s-kai", "kai")
    relay = LocalRelay(harness.factory.built[0])
    key = SessionRouting("s-kai", role=CLI_ROLE).session_key("kai")
    first = await relay.get_session(key)
    assert first is not None
    await asyncio.sleep(0.3)
    later = await relay.get_session(key)
    assert later is not None
    assert later.last_active > first.last_active


async def test_a_replaced_run_that_reconnects_is_refused_for_good(
    harness: Harness,
) -> None:
    await harness.connect("s-kai", "kai", main_agent_id="run-1")
    await harness.connect("s-kai", "kai", main_agent_id="run-2")
    with pytest.raises(DaemonError) as caught:
        await harness.connect("s-kai", "kai", main_agent_id="run-1")
    assert caught.value.code is ErrorCode.SESSION_REPLACED


async def test_a_refused_replaced_run_does_not_evict_the_current_run(
    harness: Harness,
) -> None:
    await harness.connect("s-kai", "kai", main_agent_id="run-1")
    current = await harness.connect("s-kai", "kai", main_agent_id="run-2")
    with pytest.raises(DaemonError):
        await harness.connect("s-kai", "kai", main_agent_id="run-1")
    assert cast("dict[str, object]", await current.request("who"))["error"] is False
    (session,) = harness.daemon.status().sessions
    assert session.run == "run-2"


async def test_the_refusal_names_the_run_and_the_way_forward(harness: Harness) -> None:
    await harness.connect("s-kai", "kai", main_agent_id="run-1")
    await harness.connect("s-kai", "kai", main_agent_id="run-2")
    with pytest.raises(DaemonError, match=r"run-1.*new run"):
        await harness.connect("s-kai", "kai", main_agent_id="run-1")
