"""The ``session`` verbs are daemon methods, and the daemon sweeps stale records."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest

from biff.client.connection import DaemonError
from biff.daemon.server import BiffDaemon
from biff.daemon.session_host import LEASE, RETENTION
from biff.daemon.session_store import CliSessionRecord, CliSessionStore
from biff.models import Message
from biff.run_dir import RunDir
from biff.session_id import CLI_ROLE
from biff.session_routing import SessionRouting
from biff.tty import get_hostname
from tests.test_daemon.conftest import CountingFactory, FakeResolver

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from biff.client.connection import DaemonClient
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
    sweeping: tuple[BiffDaemon, CliSessionStore, CountingFactory],
) -> None:
    daemon, store, factory = sweeping
    gone = _stored(store, "gone", "dormant", RETENTION + timedelta(minutes=1))
    await daemon.start()
    await _eventually(lambda: store.list() == ())
    assert gone.session_id not in {r.session_id for r in store.list()}
    assert factory.built != []


async def test_the_daemon_sweeps_again_on_its_timer(
    sweeping: tuple[BiffDaemon, CliSessionStore, CountingFactory],
) -> None:
    daemon, store, _ = sweeping
    await daemon.start()
    await asyncio.sleep(0.2)  # the start sweep found nothing to do
    late = _stored(store, "late", "live", LEASE + timedelta(minutes=1))
    await _eventually(lambda: store.read(late.session_id).state == "dormant")
