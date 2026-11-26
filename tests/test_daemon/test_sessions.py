"""Session records: attach, detach, a new run, conflicts, presence."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, cast

import pytest

from biff.client.connection import ConnectionLostError, DaemonError
from biff.frame_reply import ErrorCode
from biff.models import SessionEvent
from biff.relay import LocalRelay
from biff.session_id import CLI_ROLE
from biff.session_routing import SessionRouting

if TYPE_CHECKING:
    from pathlib import Path

    from tests.test_daemon.conftest import Harness


def _attached(harness: Harness, session_id: str) -> int:
    (session,) = [
        s for s in harness.daemon.status().sessions if s.session_id == session_id
    ]
    return session.attached


async def test_a_second_client_for_one_session_id_shares_the_record(
    harness: Harness,
) -> None:
    await harness.connect("s-kai", "kai", client="mcp")
    await harness.connect("s-kai", "kai", client="hook")
    assert (len(harness.daemon.status().sessions), _attached(harness, "s-kai")) == (
        1,
        2,
    )


async def test_two_shell_clients_of_one_session_id_share_the_record(
    harness: Harness,
) -> None:
    await harness.connect("s-kai", "kai", client="cli")
    await harness.connect("s-kai", "kai", client="cli")
    assert (len(harness.daemon.status().sessions), _attached(harness, "s-kai")) == (
        1,
        2,
    )


async def test_a_shell_and_an_agent_of_one_session_id_get_their_own_records(
    harness: Harness,
) -> None:
    """A shell must never read, or answer from, the inbox of the agent's session."""
    agent = await harness.connect("s-kai", "kai", client="mcp")
    shell = await harness.connect("s-kai", "kai", client="cli")
    keys = {
        cast("dict[str, dict[str, str]]", await who.request("status"))["data"][
            "session_key"
        ]
        for who in (agent, shell)
    }
    assert (len(keys), len(harness.daemon.status().sessions)) == (2, 2)


async def test_a_shell_cannot_read_mail_sent_to_the_agent_of_its_session_id(
    harness: Harness,
) -> None:
    agent = await harness.connect("s-kai", "kai", client="mcp")
    shell = await harness.connect("s-kai", "kai", client="cli")
    eric = await harness.connect("s-eric", "eric")
    status = cast("dict[str, dict[str, str]]", await agent.request("status"))
    target = status["data"]["tty_name"]
    await eric.request("write", to=f"kai:{target}", message="for the agent")
    shell_rows = cast("dict[str, list[object]]", await shell.request("read"))["data"]
    assert shell_rows == []


async def test_two_clients_of_one_session_see_one_plan(harness: Harness) -> None:
    first = await harness.connect("s-kai", "kai", client="mcp")
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


class HistoryRelay(LocalRelay):
    """A local relay that keeps session history, as the NATS relay does."""

    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)
        self.events: list[SessionEvent] = []

    async def append_wtmp(self, event: SessionEvent) -> None:
        self.events.append(event)

    async def get_wtmp(
        self, *, user: str | None = None, count: int = 25
    ) -> list[SessionEvent]:
        return [e for e in self.events if user in (None, e.user)][-count:]


@pytest.fixture
def history(harness: Harness) -> list[HistoryRelay]:
    made: list[HistoryRelay] = []

    def build(directory: Path) -> HistoryRelay:
        relay = HistoryRelay(directory)
        made.append(relay)
        return relay

    harness.factory.relay_type = build
    return made


async def test_a_session_the_daemon_creates_records_a_login(
    harness: Harness, history: list[HistoryRelay]
) -> None:
    await harness.connect("s-kai", "kai")
    key = SessionRouting("s-kai", role=CLI_ROLE).session_key("kai")
    assert [(e.event, e.session_key) for e in history[0].events] == [("login", key)]


async def test_last_lists_a_session_the_daemon_created(
    harness: Harness, history: list[HistoryRelay]
) -> None:
    kai = await harness.connect("s-kai", "kai")
    result = cast("dict[str, object]", await kai.request("last"))
    assert "kai" in json.dumps(result["data"])
    assert [e.event for e in history[0].events] == ["login"]


async def test_reattaching_does_not_record_a_second_login(
    harness: Harness, history: list[HistoryRelay]
) -> None:
    first = await harness.connect("s-kai", "kai")
    await first.close()
    await asyncio.sleep(0.05)
    await harness.connect("s-kai", "kai")
    assert len(history[0].events) == 1


async def test_a_recreated_row_records_a_new_login(
    harness: Harness, history: list[HistoryRelay]
) -> None:
    first = await harness.connect("s-kai", "kai")
    await first.close()
    await asyncio.sleep(0.05)
    key = SessionRouting("s-kai", role=CLI_ROLE).session_key("kai")
    await history[0].delete_session(key)
    await harness.connect("s-kai", "kai")
    assert len(history[0].events) == 2


async def test_a_failing_history_write_does_not_fail_the_hello(
    harness: Harness, history: list[HistoryRelay], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: HistoryRelay, event: SessionEvent) -> None:
        msg = "history store down"
        raise OSError(msg)

    monkeypatch.setattr(HistoryRelay, "append_wtmp", broken)
    kai = await harness.connect("s-kai", "kai")
    assert cast("dict[str, object]", await kai.request("who"))["error"] is False
