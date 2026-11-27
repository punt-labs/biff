"""The ``session`` verbs are daemon methods, and the daemon sweeps stale records."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from biff.client.connection import (
    ClientIdentity,
    ConnectionLostError,
    DaemonClient,
    DaemonError,
)
from biff.daemon.server import BiffDaemon
from biff.daemon.session_host import LEASE, RETENTION, SessionHost
from biff.daemon.session_pointer import CurrentPointer
from biff.daemon.session_select import SessionSelector
from biff.daemon.session_store import CliSessionRecord, CliSessionStore
from biff.daemon.session_verbs import SessionVerbs
from biff.models import BiffConfig, Message
from biff.relay import LocalRelay
from biff.run_dir import RunDir
from biff.session_id import CLI_ROLE
from biff.session_routing import SessionRouting
from biff.tty import get_hostname
from tests.test_daemon.conftest import CountingFactory, FakeResolver
from tests.test_daemon.test_read_delivery import ConsumingRelay

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from biff.commands import CommandResult
    from biff.daemon.session_store import SessionState
    from tests.test_daemon.conftest import Harness

type Reply = dict[str, object]

_WAIT = 5.0


async def _call(client: DaemonClient, method: str, **params: str | bool) -> Reply:
    return cast("Reply", await client.request(method, **params))


def _key(record: CliSessionRecord) -> str:
    return SessionRouting(record.session_id, role=CLI_ROLE).session_key(record.user)


async def test_session_new_creates_a_record_and_prints_the_name(
    harness: Harness,
) -> None:
    kai = await harness.connect("s-kai")
    reply = await _call(kai, "session_new", tty="main")
    (record,) = (r for r in harness.sessions.list() if r.tty == "main")
    assert (reply["text"], reply["error"]) == ("main", False)
    assert reply["data"] == {
        "name": "main",
        "user": "kai",
        "repo": "test",
        "state": "live",
        "last_attached": record.last_attached_at.isoformat(timespec="seconds"),
        "id": record.session_id,
    }


async def test_session_new_lists_the_new_session_in_who(harness: Harness) -> None:
    kai = await harness.connect("s-kai")
    await _call(kai, "session_new", tty="main")
    who = await _call(kai, "who")
    assert "main" in str(who["text"])


async def test_a_taken_session_name_is_an_error_and_creates_nothing(
    harness: Harness,
) -> None:
    kai = await harness.connect("s-kai")
    await _call(kai, "session_new", tty="main")
    reply = await _call(kai, "session_new", tty="main")
    assert reply["error"] is True
    assert "already in use" in str(reply["text"])
    assert [r.tty for r in harness.sessions.list()] == ["main"]


async def test_session_ls_prints_the_table_of_this_hosts_sessions(
    harness: Harness,
) -> None:
    kai = await harness.connect("s-kai")
    await _call(kai, "session_new", tty="main")
    reply = await _call(kai, "session_ls")
    text = str(reply["text"])
    assert text.splitlines()[0].startswith("NAME")
    assert "main" in text


async def test_session_ls_hides_ended_sessions_unless_asked(harness: Harness) -> None:
    kai = await harness.connect("s-kai")
    await _call(kai, "session_new", tty="main")
    await _call(kai, "session_end", ref="main")
    hidden = await _call(kai, "session_ls")
    shown = await _call(kai, "session_ls", include_ended=True)
    assert (hidden["text"], "ended" in str(shown["text"])) == ("No sessions.", True)


async def test_session_use_names_the_session_and_changes_nothing(
    harness: Harness,
) -> None:
    kai = await harness.connect("s-kai")
    await _call(kai, "session_new", tty="alpha")
    (record,) = harness.sessions.list()
    harness.sessions.lapse(record.session_id)
    reply = await _call(kai, "session_use", ref="alpha")
    data = cast("dict[str, str]", reply["data"])
    assert (reply["text"], data["id"]) == ("alpha", record.session_id)
    assert harness.sessions.read(record.session_id).state == "dormant"


async def test_session_use_of_an_unknown_name_is_an_error(harness: Harness) -> None:
    kai = await harness.connect("s-kai")
    reply = await _call(kai, "session_use", ref="nope")
    assert (reply["error"], reply["text"]) == (True, "Error: unknown session: nope")


async def test_session_end_drains_unread_mail_and_releases_the_row(
    harness: Harness,
) -> None:
    kai = await harness.connect("s-kai")
    eric = await harness.connect("s-eric", "eric")
    await _call(kai, "session_new", tty="alpha")
    (record,) = harness.sessions.list()
    relay = harness.factory.relay_type(harness.factory.built[0])
    await relay.deliver(
        Message(from_user="eric", from_tty="tty2", to_user=_key(record), body="hello")
    )
    reply = await _call(kai, "session_end", ref="alpha")
    assert (reply["text"], reply["error"]) == ("ended alpha", False)
    assert harness.sessions.read(record.session_id).state == "ended"
    assert await relay.get_session(_key(record)) is None
    assert [m.body for m in await relay.fetch_user_inbox("kai")] == ["hello"]
    await eric.close()


async def test_ending_an_ended_session_is_an_error(harness: Harness) -> None:
    kai = await harness.connect("s-kai")
    await _call(kai, "session_new", tty="alpha")
    await _call(kai, "session_end", ref="alpha")
    reply = await _call(kai, "session_end", ref="alpha")
    assert reply["error"] is True


async def test_a_session_method_refuses_a_param_it_does_not_take(
    harness: Harness,
) -> None:
    kai = await harness.connect("s-kai")
    with pytest.raises(DaemonError, match="unknown param bogus"):
        await _call(kai, "session_ls", bogus="x")


@pytest.fixture
async def sweeping(
    tmp_path: Path, run_dir: RunDir
) -> AsyncIterator[tuple[BiffDaemon, CliSessionStore, CountingFactory]]:
    """A daemon, not yet started, sweeping every 50 ms over a store of its own."""
    store = CliSessionStore(tmp_path / "sessions")
    factory = CountingFactory()
    daemon = BiffDaemon(
        run_dir=run_dir,
        port=0,
        sessions=store,
        resolver=FakeResolver(tmp_path / "relays"),
        relay_factory=factory,
        session_sweep_interval=0.05,
    )
    yield daemon, store, factory
    await daemon.stop()


@pytest.fixture
def run_dir(tmp_path: Path) -> RunDir:
    return RunDir(tmp_path / "run")


async def _eventually(check: Callable[[], bool]) -> None:
    deadline = asyncio.get_running_loop().time() + _WAIT
    while not check():
        assert asyncio.get_running_loop().time() < deadline, "never became true"
        await asyncio.sleep(0.02)


def _stored(
    store: CliSessionStore, tty: str, state: SessionState, ago: timedelta
) -> CliSessionRecord:
    now = datetime.now(UTC)
    record = replace(
        CliSessionRecord.new(
            tty=tty,
            user="kai",
            repo="test",
            repo_root="/work/repo",
            hostname=get_hostname(),
            now=now,
        ),
        state=state,
        last_attached_at=now - ago,
    )
    store.create(record)
    return record


async def test_the_daemon_sweeps_a_stale_live_record_to_dormant_at_start(
    sweeping: tuple[BiffDaemon, CliSessionStore, CountingFactory],
) -> None:
    daemon, store, _ = sweeping
    stale = _stored(store, "old", "live", LEASE + timedelta(minutes=1))
    fresh = _stored(store, "new", "live", timedelta(minutes=1))
    await daemon.start()
    await _eventually(lambda: store.read(stale.session_id).state == "dormant")
    assert store.read(fresh.session_id).state == "live"


async def test_the_daemon_expires_a_record_past_retention_and_releases_its_row(
    sweeping: tuple[BiffDaemon, CliSessionStore, CountingFactory], tmp_path: Path
) -> None:
    daemon, store, _ = sweeping
    relay = LocalRelay(tmp_path / "relays" / "repo")
    host = SessionHost(
        relay=relay,
        config=BiffConfig(user="kai", repo_name="test"),
        selector=SessionSelector(
            store=store,
            pointer=CurrentPointer(tmp_path / "current"),
            user="kai",
            repo="test",
            repo_root=Path("/work/repo"),
            hostname=get_hostname(),
        ),
        hostname=get_hostname(),
        pwd="/work/repo",
    )
    made = await host.create("gone")
    key = _key(made)
    assert await relay.get_session(key) is not None
    path = store.root / made.session_id / "session.json"
    old = datetime.now(UTC) - RETENTION - timedelta(minutes=1)
    path.write_text(replace(made, state="dormant", last_attached_at=old).to_json())
    await daemon.start()
    await _eventually(lambda: store.list() == ())
    assert await relay.get_session(key) is None
    assert await relay.get_tty_reservation_owner("kai", "gone") is None


async def test_the_daemon_sweeps_again_on_its_timer(
    sweeping: tuple[BiffDaemon, CliSessionStore, CountingFactory],
) -> None:
    daemon, store, _ = sweeping
    await daemon.start()
    await asyncio.sleep(0.2)  # the start sweep found nothing to do
    late = _stored(store, "late", "live", LEASE + timedelta(minutes=1))
    await _eventually(lambda: store.read(late.session_id).state == "dormant")


async def test_the_sweep_renews_a_session_a_connection_is_attached_to(
    sweeping: tuple[BiffDaemon, CliSessionStore, CountingFactory], run_dir: RunDir
) -> None:
    daemon, store, _ = sweeping
    stale = _stored(store, "held", "live", RETENTION + timedelta(minutes=1))
    await daemon.start()
    watcher = await DaemonClient.connect(
        run_dir=run_dir,
        port=daemon.port,
        identity=ClientIdentity(
            client="cli", session_id=stale.session_id, cwd="/work/repo", user="kai"
        ),
    )
    try:
        await daemon.sweep_sessions()
        kept = store.read(stale.session_id)
    finally:
        await watcher.close()
    assert kept.state == "live"
    assert datetime.now(UTC) - kept.last_attached_at < timedelta(minutes=1)


async def _bodies_a_new_session_reads(harness: Harness) -> list[str]:
    """Read everything a session that did not exist before the end can read."""
    fresh = await harness.connect("s-fresh", "kai")
    bodies: list[str] = []
    for _ in range(10):
        data = cast("list[dict[str, str]]", (await _call(fresh, "read"))["data"])
        if not data:
            break
        bodies += [row["body"] for row in data]
    return bodies


async def test_ending_a_session_files_back_a_read_nobody_confirmed(
    harness: Harness,
) -> None:
    harness.factory.relay_type = ConsumingRelay
    kai = await harness.connect("s-kai")
    await _call(kai, "session_new", tty="alpha")
    (record,) = (r for r in harness.sessions.list() if r.tty == "alpha")
    holder = await harness.connect(record.session_id, "kai")
    relay = ConsumingRelay(harness.factory.built[0])
    await relay.deliver(
        Message(from_user="eric", from_tty="t", to_user=_key(record), body="held")
    )
    unconfirmed = await holder.request_held("read")  # fetched, never confirmed
    assert unconfirmed.result is not None
    await _call(kai, "session_end", ref="alpha")
    assert await _bodies_a_new_session_reads(harness) == ["held"]


async def test_ending_a_session_files_back_the_batch_held_after_a_split_read(
    harness: Harness,
) -> None:
    harness.factory.relay_type = ConsumingRelay
    harness.daemon._core.methods._read_budget = 1400  # one message per reply
    kai = await harness.connect("s-kai")
    await _call(kai, "session_new", tty="alpha")
    (record,) = (r for r in harness.sessions.list() if r.tty == "alpha")
    holder = await harness.connect(record.session_id, "kai")
    relay = ConsumingRelay(harness.factory.built[0])
    for n in range(3):
        await relay.deliver(
            Message(
                from_user="eric",
                from_tty="t",
                to_user=_key(record),
                body=f"m{n} " + "x" * 300,
            )
        )
    unconfirmed = await holder.request_held("read")  # a batch of one, unconfirmed
    assert unconfirmed.result is not None
    await _call(kai, "session_end", ref="alpha")
    bodies = await _bodies_a_new_session_reads(harness)
    assert sorted(b[:2] for b in bodies) == ["m0", "m1", "m2"]


async def test_ending_a_session_ends_the_connections_attached_to_it(
    harness: Harness,
) -> None:
    kai = await harness.connect("s-kai")
    await _call(kai, "session_new", tty="alpha")
    (record,) = (r for r in harness.sessions.list() if r.tty == "alpha")
    watcher = await harness.connect(record.session_id, "kai")
    await watcher.subscribe()
    await asyncio.wait_for(watcher.next_event(), timeout=_WAIT)
    await _call(kai, "session_end", ref="alpha")
    with pytest.raises(ConnectionLostError, match="session ended"):
        await asyncio.wait_for(watcher.next_event(), timeout=_WAIT)


async def test_a_session_can_end_itself_and_still_hear_the_answer(
    harness: Harness,
) -> None:
    kai = await harness.connect("s-kai")
    await _call(kai, "session_new", tty="alpha")
    (record,) = (r for r in harness.sessions.list() if r.tty == "alpha")
    own = await harness.connect(record.session_id, "kai")
    reply = await _call(own, "session_end", ref="alpha")
    assert (reply["text"], reply["error"]) == ("ended alpha", False)


async def test_each_session_verb_prints_through_biffd_what_it_prints_in_process(
    harness: Harness, tmp_path: Path
) -> None:
    kai = await harness.connect("s-kai")
    await _call(kai, "session_new", tty="main")
    relay = LocalRelay(harness.factory.built[0])
    hostname = get_hostname()
    host = SessionHost(
        relay=relay,
        config=BiffConfig(user="kai", repo_name="test"),
        selector=SessionSelector(
            store=harness.sessions,
            pointer=CurrentPointer(tmp_path / "current"),
            user="kai",
            repo="test",
            repo_root=Path("/work/repo"),
            hostname=hostname,
        ),
        hostname=hostname,
        pwd="/work/repo",
    )
    verbs = SessionVerbs(host)
    table: list[tuple[str, dict[str, str | bool], Awaitable[CommandResult]]] = [
        ("session_ls", {}, verbs.ls(include_ended=False)),
        ("session_ls", {"include_ended": True}, verbs.ls(include_ended=True)),
        ("session_use", {"ref": "main"}, verbs.use("main")),
    ]
    for method, params, in_process in table:
        reply = await _call(kai, method, **params)
        expected = await in_process
        assert (method, reply["text"], reply["data"]) == (
            method,
            expected.text,
            expected.json_data,
        )
    assert (await _call(kai, "session_end", ref="main"))["text"] == "ended main"
    refusals: list[tuple[str, str, Awaitable[CommandResult]]] = [
        ("session_use", "nope", verbs.use("nope")),
        ("session_end", "main", verbs.end("main")),  # already ended
    ]
    for method, ref, in_process in refusals:
        reply = await _call(kai, method, ref=ref)
        with pytest.raises(ValueError) as refused:
            await in_process
        assert (method, reply["text"]) == (method, f"Error: {refused.value}")
