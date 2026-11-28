"""The spill is the only copy of mail the relay gave up: no error path may lose it."""

from __future__ import annotations

import asyncio
import dataclasses
import errno
import json
import logging
import pathlib
import stat
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ClassVar

import pytest

from biff.commands._result import CommandResult
from biff.daemon.durable_file import DurableFile, UnsyncedError
from biff.daemon.handback import Handback
from biff.daemon.outbox import Outbox
from biff.daemon.server import BiffDaemon
from biff.daemon.spill import Spill
from biff.daemon.spill_entry import Held, Spilled
from biff.daemon.spill_file import SpillFile
from biff.models import Message
from biff.nats_relay import (
    _INBOX_DUPLICATE_WINDOW_SECONDS,  # pyright: ignore[reportPrivateUsage]
)
from biff.relay import LocalRelay
from biff.run_dir import RunDir
from tests.test_daemon.claude_seats import until
from tests.test_daemon.conftest import CountingFactory, FakeResolver
from tests.test_daemon.consuming import ConsumingRelay

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from biff.daemon.sessions import Session
    from tests.test_daemon.conftest import Harness


class PickyRelay(ConsumingRelay):
    """A consuming relay that refuses one body and can be slow."""

    poison = ""
    crash = ""
    pace = 0.0

    async def deliver(
        self,
        message: Message,
        *,
        sender_key: str = "",
        target_repo: str | None = None,
    ) -> None:
        if type(self).poison and message.body == type(self).poison:
            msg = "the relay refuses this message"
            raise OSError(msg)
        if type(self).crash and message.body == type(self).crash:
            msg = "a bug in the relay, not an error it reports"
            raise ValueError(msg)
        await asyncio.sleep(type(self).pace)
        await super().deliver(message, sender_key=sender_key, target_repo=target_repo)


def _message(body: str) -> Message:
    return Message(from_user="eric", to_user="kai", body=body)


def _message_to(address: str, body: str) -> Message:
    return Message(from_user="eric", to_user=address, body=body)


def _spilled(session_id: str, *bodies: str, since: datetime | None = None) -> Spilled:
    return Spilled(
        session_id=session_id,
        agent=False,
        cwd="/work/repo",
        user="kai",
        held=tuple(
            Held(message=_message(b))
            if since is None
            else Held(message=_message(b), since=since)
            for b in bodies
        ),
    )


def _files(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "spill.json", tmp_path / "run" / "spill.json"


def _spill(tmp_path: Path) -> Spill:
    return Spill(*_files(tmp_path))


def _seed(tmp_path: Path, *entries: Spilled) -> None:
    SpillFile(*_files(tmp_path)).write({e.key: e for e in entries})


async def _session(harness: Harness) -> Session:
    harness.factory.relay_type = PickyRelay
    await harness.connect("s-kai", "kai")
    (session,) = harness.daemon._core.sessions.snapshot().values()  # pyright: ignore[reportPrivateUsage]
    return session


async def _mailbox(harness: Harness) -> list[str]:
    viewer = LocalRelay(harness.relays_dir / "repo")
    return sorted(m.body for m in await viewer.fetch_user_inbox("kai"))


def _bodies(messages: Sequence[Message]) -> list[str]:
    return sorted(m.body for m in messages)


# -- the file ---------------------------------------------------------------


async def test_a_spill_is_one_private_file_and_a_message_is_in_it_once(
    harness: Harness, tmp_path: Path
) -> None:
    session = await _session(harness)
    spill = _spill(tmp_path)
    mail = _message("m1")

    spill.hold(session, [mail])
    spill.hold(session, [mail])  # held twice, kept once

    path = _files(tmp_path)[0]
    assert (stat.S_IMODE(path.stat().st_mode), path.read_text().count('"m1"')) == (
        0o600,
        1,
    )


async def test_an_unreadable_file_is_set_aside_each_time_under_its_own_name(
    harness: Harness, tmp_path: Path
) -> None:
    session = await _session(harness)
    path = _files(tmp_path)[0]
    path.write_text("{first")
    _spill(tmp_path).hold(session, [_message("a")])
    path.write_text("{second")
    _spill(tmp_path).hold(session, [_message("b")])

    asides = sorted(p.read_text() for p in tmp_path.glob("spill.unreadable-*.json"))
    assert asides == ["{first", "{second"]


async def test_a_spill_path_that_cannot_be_read_never_stops_the_caller(
    harness: Harness, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _files(tmp_path)[0]
    path.mkdir()  # reading it raises IsADirectoryError, an OSError

    with caplog.at_level(logging.ERROR, logger="biff.daemon.spill_file"):
        spill = _spill(tmp_path)
        await spill.refile(harness.daemon._core)  # pyright: ignore[reportPrivateUsage]
        pending = spill.pending()

    assert (pending, "cannot be read" in caplog.text) == (0, True)


async def test_one_bad_entry_does_not_quarantine_the_good_ones(
    harness: Harness, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _seed(tmp_path, _spilled("s-good", "kept"))
    path = _files(tmp_path)[0]
    data = json.loads(path.read_text())
    data["spilled"].append({"session_id": 3, "held": [{}, {}]})
    path.write_text(json.dumps(data))

    with caplog.at_level(logging.ERROR, logger="biff.daemon.spill_file"):
        pending = _spill(tmp_path).pending()

    (aside,) = tmp_path.glob("spill.corrupt-*.json")
    assert (
        pending,
        len(json.loads(aside.read_text())["spilled"]),
        "1 spilled sessions (2 messages)" in caplog.text,
        [e["session_id"] for e in json.loads(path.read_text())["spilled"]],
    ) == (1, 1, True, ["s-good"])


async def test_fields_a_newer_biff_added_are_ignored(tmp_path: Path) -> None:
    _seed(tmp_path, _spilled("s-one", "kept"))
    path = _files(tmp_path)[0]
    data = json.loads(path.read_text())
    data["written_by"] = "biff 99"
    data["spilled"][0]["priority"] = "high"
    path.write_text(json.dumps(data))

    assert _spill(tmp_path).pending() == 1


# -- writing -----------------------------------------------------------------


async def test_a_spill_that_cannot_be_written_in_the_first_place_goes_to_the_second(
    harness: Harness, tmp_path: Path
) -> None:
    session = await _session(harness)
    first = tmp_path / "sealed"
    first.mkdir()
    first.chmod(stat.S_IRUSR | stat.S_IXUSR)
    spill = Spill(first / "spill.json", tmp_path / "run" / "spill.json")
    try:
        spill.hold(session, [_message("m1")])
    finally:
        first.chmod(stat.S_IRWXU)

    assert (tmp_path / "run" / "spill.json").exists()
    again = Spill(first / "spill.json", tmp_path / "run" / "spill.json")
    assert again.pending() == 1  # the next start finds it there


async def test_a_spill_that_cannot_be_written_anywhere_is_logged_critical_and_kept(
    harness: Harness, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    session = await _session(harness)
    spill = _spill(tmp_path)
    spill.hold(session, [_message("safe")])
    for directory in (tmp_path / "run", tmp_path):
        directory.mkdir(exist_ok=True)
        directory.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with caplog.at_level(logging.CRITICAL, logger="biff.daemon.spill"):
            spill.hold(session, [_message("lost")])
    finally:
        for directory in (tmp_path / "run", tmp_path):
            directory.chmod(stat.S_IRWXU)

    # The batch that could not be written is kept in memory for the next write.
    assert (spill.pending(), "LOST unless found here" in caplog.text) == (2, True)


async def test_a_spill_whose_directory_could_not_be_synced_is_a_written_spill(
    harness: Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = await _session(harness)

    def failing(_directory: Path) -> None:
        msg = "no sync"
        raise OSError(msg)

    monkeypatch.setattr(DurableFile, "_sync_directory", staticmethod(failing))
    with caplog.at_level(logging.WARNING):
        _spill(tmp_path).hold(session, [_message("m1")])

    assert (_spill(tmp_path).pending(), "LOST" in caplog.text) == (1, False)


def test_a_failed_directory_sync_is_told_apart_from_a_failed_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(_directory: Path) -> None:
        msg = "no sync"
        raise OSError(msg)

    monkeypatch.setattr(DurableFile, "_sync_directory", staticmethod(failing))
    with pytest.raises(UnsyncedError):
        DurableFile.replace(tmp_path, "f.json", "text")

    assert (tmp_path / "f.json").read_text() == "text"


# -- filing it back ----------------------------------------------------------


async def test_filing_back_never_stops_at_one_bad_session(
    harness: Harness, tmp_path: Path
) -> None:
    await _session(harness)
    _seed(tmp_path, _spilled("bad id!", "stuck"), _spilled("s-fine", "filed"))

    spill = _spill(tmp_path)
    await spill.refile(harness.daemon._core)  # pyright: ignore[reportPrivateUsage]

    assert (await _mailbox(harness), spill.pending()) == (["filed"], 1)


async def test_filing_back_is_bounded_in_total_and_keeps_what_it_did_not_reach(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _session(harness)
    monkeypatch.setattr(Spill, "REFILE_SECONDS", 0.2)
    monkeypatch.setattr(PickyRelay, "pace", 0.15)
    _seed(tmp_path, _spilled("s-1", "m1"), _spilled("s-2", "m2"), _spilled("s-3", "m3"))

    spill = _spill(tmp_path)
    async with asyncio.timeout(0.6):
        await spill.refile(harness.daemon._core)  # pyright: ignore[reportPrivateUsage]

    left = SpillFile(*_files(tmp_path)).read()
    assert (await _mailbox(harness), sorted(k[0] for k in left)) == (
        ["m1"],
        ["s-2", "s-3"],
    )


async def test_a_message_the_relay_always_refuses_does_not_hold_back_the_ones_after_it(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _session(harness)
    monkeypatch.setattr(PickyRelay, "poison", "poison")
    _seed(tmp_path, _spilled("s-one", "poison", "after"))

    spill = _spill(tmp_path)
    await spill.refile(harness.daemon._core)  # pyright: ignore[reportPrivateUsage]

    (left,) = SpillFile(*_files(tmp_path)).read().values()
    assert (await _mailbox(harness), _bodies(left.messages)) == (["after"], ["poison"])


async def test_the_spill_is_counted_at_start_and_shown_in_the_status(
    harness: Harness, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    await _session(harness)
    _seed(tmp_path, _spilled("s-1", "a", "b"), _spilled("s-2", "c"))
    spill = _spill(tmp_path)

    shown = spill.annotate(CommandResult(text="biff", json_data={"user": "kai"}))
    with caplog.at_level(logging.WARNING, logger="biff.daemon.spill"):
        await spill.refile(harness.daemon._core)  # pyright: ignore[reportPrivateUsage]

    assert (
        shown.json_data,
        "spilled: 3 messages pending" in shown.text,
        "3 spilled messages pending for 2 sessions" in caplog.messages,
        spill.annotate(CommandResult(text="biff")).text,
    ) == ({"user": "kai", "spilled_messages": 3}, True, True, "biff")


# -- the sealed outbox -------------------------------------------------------


async def test_a_sealed_outbox_of_a_relay_that_keeps_mail_spills_nothing() -> None:
    outbox = Outbox(retains=False)
    spilled: list[Message] = []

    outbox.seal(spilled.extend)
    outbox.merge([_message("m1")])

    assert spilled == []


async def test_a_session_made_after_the_seal_is_sealed_too(harness: Harness) -> None:
    harness.factory.relay_type = PickyRelay
    sunk: list[tuple[str, list[str]]] = []

    def sink(session: Session, messages: Sequence[Message]) -> None:
        sunk.append((session.session_id, _bodies(messages)))

    harness.daemon._core.sessions.seal(sink)  # pyright: ignore[reportPrivateUsage]
    await harness.connect("s-late", "kai")
    (session,) = harness.daemon._core.sessions.snapshot().values()  # pyright: ignore[reportPrivateUsage]
    session.outbox.merge([_message("late")])

    assert sunk == [("s-late", ["late"])]


# -- the durable file --------------------------------------------------------


def test_a_durable_file_is_replaced_whole_and_private(tmp_path: Path) -> None:
    directory = tmp_path / "private"

    DurableFile.replace(directory, "file.json", "one")
    DurableFile.replace(directory, "file.json", "two")

    path = directory / "file.json"
    assert (
        path.read_text(),
        stat.S_IMODE(path.stat().st_mode),
        stat.S_IMODE(directory.stat().st_mode),
        [p.name for p in directory.iterdir()],
    ) == ("two", 0o600, 0o700, ["file.json"])


def test_a_failed_replace_leaves_the_old_file_and_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    DurableFile.replace(tmp_path, "file.json", "old")

    def failing(_fd: int) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr("os.fsync", failing)
    with pytest.raises(OSError, match="disk full"):
        DurableFile.replace(tmp_path, "file.json", "new")

    left = [p.name for p in tmp_path.iterdir()]
    assert ((tmp_path / "file.json").read_text(), left) == ("old", ["file.json"])


async def test_spilled_mail_of_a_session_that_ended_goes_to_its_users_mailbox(
    harness: Harness, tmp_path: Path
) -> None:
    await _session(harness)
    ended = Spilled(
        session_id="s-gone",
        agent=False,
        cwd="/work/repo",
        user="kai",
        session_key="kai:no-row-for-this",  # the relay lists no such session
        held=(Held(message=_message_to("kai:ghost", "dead")),),
    )
    _seed(tmp_path, ended)

    spill = _spill(tmp_path)
    await spill.refile(harness.daemon._core)  # pyright: ignore[reportPrivateUsage]

    assert (await _mailbox(harness), spill.pending()) == (["dead"], 0)


async def test_spilled_mail_is_not_filed_in_a_relay_the_repository_names_now(
    harness: Harness, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    await _session(harness)
    elsewhere = _spilled("s-moved", "stays").model_copy(
        update={"relay": "nats://old-server|repo"}
    )
    _seed(tmp_path, elsewhere)

    spill = _spill(tmp_path)
    with caplog.at_level(logging.ERROR, logger="biff.daemon.spill"):
        await spill.refile(harness.daemon._core)  # pyright: ignore[reportPrivateUsage]

    kept = "kept, not filed" in caplog.text
    assert (await _mailbox(harness), spill.pending(), kept) == ([], 1, True)


async def test_a_stale_fallback_that_cannot_be_deleted_costs_the_next_session_nothing(
    harness: Harness, tmp_path: Path
) -> None:
    session = await _session(harness)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "spill.json").write_text(json.dumps({"spilled": []}))
    primary = tmp_path / "spill.json"
    spill = Spill(primary, elsewhere / "spill.json")
    elsewhere.chmod(stat.S_IRUSR | stat.S_IXUSR)  # the stale fallback stays
    try:
        spill.hold(session, [_message("a")])
        spill.hold(session, [_message("b")])
    finally:
        elsewhere.chmod(stat.S_IRWXU)

    assert Spill(primary, elsewhere / "spill.json").pending() == 2


async def test_a_stale_primary_is_not_merged_with_the_newer_fallback(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await _session(harness)
    monkeypatch.setattr(PickyRelay, "poison", "y")
    spill = _spill(tmp_path)
    spill.hold(session, [_message("x"), _message("y")])
    tmp_path.chmod(stat.S_IRUSR | stat.S_IXUSR)  # the primary cannot be replaced
    try:
        await spill.refile(harness.daemon._core)  # pyright: ignore[reportPrivateUsage]
    finally:
        tmp_path.chmod(stat.S_IRWXU)

    # x was filed and y stays; the next start must not see x again.
    assert _spill(tmp_path).pending() == 1


async def test_fresh_mail_is_not_shelved_with_the_old_mail_of_its_session(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _session(harness)
    monkeypatch.setattr(PickyRelay, "poison", "poison")
    old = datetime.now(UTC) - timedelta(days=8)
    tried = Spill.UNDELIVERABLE_ATTEMPTS
    entry = _spilled("s-one").keeping(
        (
            Held(
                message=_message("poison"),
                since=old,
                attempts=tried,
                failing_for=timedelta(days=8).total_seconds(),
            ),
            Held(message=_message("poison")),  # spilled just now
        )
    )
    _seed(tmp_path, entry)

    spill = _spill(tmp_path)
    await spill.refile(harness.daemon._core)  # pyright: ignore[reportPrivateUsage]

    assert (spill.pending(), spill.undeliverable()) == (1, 1)


async def test_shelved_mail_stays_counted_in_the_status_and_is_reported_at_start(
    harness: Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _session(harness)
    monkeypatch.setattr(PickyRelay, "poison", "poison")
    SpillFile(*_files(tmp_path)).shelve(_spilled("s-one", "poison"))

    again = _spill(tmp_path)  # the next start
    shown = again.annotate(CommandResult(text="biff", json_data={"user": "kai"}))
    with caplog.at_level(logging.ERROR, logger="biff.daemon.spill"):
        await again.resume(harness.daemon._core)  # pyright: ignore[reportPrivateUsage]

    assert (
        "undeliverable: 1 spilled messages are shelved" in shown.text,
        shown.json_data,
        "1 spilled messages were shelved as undeliverable" in caplog.text,
    ) == (True, {"user": "kai", "undeliverable_messages": 1}, True)


async def test_no_log_line_carries_the_userinfo_of_a_relay_url(
    harness: Harness, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    await _session(harness)
    secret = Spilled.relay_id("nats://alice:s3cret-token@old-server:4222", "repo")
    entry = _spilled("s-moved", "stays").model_copy(update={"relay": secret})
    _seed(tmp_path, entry)

    with caplog.at_level(logging.DEBUG):
        await _spill(tmp_path).refile(harness.daemon._core)  # pyright: ignore[reportPrivateUsage]

    text = caplog.text + (_files(tmp_path)[0]).read_text()
    assert ("old-server:4222" in text, "s3cret" in text, "alice" in text) == (
        True,
        False,
        False,
    )


async def test_the_newer_snapshot_wins_whichever_file_holds_it(tmp_path: Path) -> None:
    primary, fallback = _files(tmp_path)
    older = SpillFile(primary, fallback)
    older.write({("s-one", False): _spilled("s-one", "x", "y")})
    newer = SpillFile(primary, fallback)
    newer.read()
    text = json.loads(primary.read_text())
    only_y = _spilled("s-one", "y").model_dump(mode="json")
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text(json.dumps({"seq": text["seq"] + 1, "spilled": [only_y]}))

    (item,) = SpillFile(primary, fallback).read().values()

    assert [m.body for m in item.messages] == ["y"]


# -- retrying, aging, visibility ---------------------------------------------


async def test_mail_spilled_while_the_network_was_down_is_filed_when_it_returns(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _session(harness)
    monkeypatch.setattr(PickyRelay, "poison", "m1")
    monkeypatch.setattr(Spill, "RETRY_DELAYS", (0.02,))
    monkeypatch.setattr(Spill, "RETRY_INTERVAL", 0.02)
    _seed(tmp_path, _spilled("s-one", "m1"))
    spill = _spill(tmp_path)
    await spill.resume(harness.daemon._core)  # pyright: ignore[reportPrivateUsage]
    assert await _mailbox(harness) == []  # the relay refused it at start

    keeping = asyncio.create_task(
        spill.keep_filing(harness.daemon._core)  # pyright: ignore[reportPrivateUsage]
    )
    try:
        monkeypatch.setattr(PickyRelay, "poison", "")  # the network is back
        await until(lambda: spill.pending() == 0)
    finally:
        keeping.cancel()
        await asyncio.gather(keeping, return_exceptions=True)

    assert await _mailbox(harness) == ["m1"]


async def test_a_daemon_started_before_the_network_files_the_spill_when_it_returns(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _session(harness)
    monkeypatch.setattr(PickyRelay, "poison", "m1")
    monkeypatch.setattr(Spill, "RETRY_DELAYS", (0.02,))
    monkeypatch.setattr(Spill, "RETRY_INTERVAL", 0.02)
    run_dir = RunDir(tmp_path / "run-login")
    SpillFile(
        harness.sessions.root.parent / "handback-spill.json",
        run_dir.path / "handback-spill.json",
    ).write({("s-one", False): _spilled("s-one", "m1")})
    factory = CountingFactory()
    factory.relay_type = PickyRelay
    daemon = BiffDaemon(
        run_dir=run_dir,
        port=0,
        sessions=harness.sessions,
        resolver=FakeResolver(harness.relays_dir),
        relay_factory=factory,
        request_timeout=0.5,
    )
    await daemon.start()  # the relay refuses everything: no network yet
    try:
        assert daemon.status().spilled == 1
        monkeypatch.setattr(PickyRelay, "poison", "")  # the network is up
        await until(lambda: daemon.status().spilled == 0)
    finally:
        await daemon.stop()

    assert await _mailbox(harness) == ["m1"]


async def test_one_failed_try_after_a_week_without_the_daemon_does_not_shelve_mail(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _session(harness)
    monkeypatch.setattr(PickyRelay, "poison", "poison")
    old = datetime.now(UTC) - timedelta(days=8)
    _seed(tmp_path, _spilled("s-one", "poison", since=old))

    spill = _spill(tmp_path)
    await spill.refile(harness.daemon._core)  # pyright: ignore[reportPrivateUsage]

    assert (spill.pending(), spill.undeliverable()) == (1, 0)


async def test_mail_that_failed_many_times_over_a_week_is_shelved(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _session(harness)
    monkeypatch.setattr(PickyRelay, "poison", "poison")
    old = datetime.now(UTC) - timedelta(days=8)
    tried = Held(
        message=_message("poison"),
        since=old,
        attempts=Spill.UNDELIVERABLE_ATTEMPTS - 1,
        failing_for=timedelta(days=8).total_seconds(),
    )
    _seed(tmp_path, _spilled("s-one").keeping((tried,)))

    spill = _spill(tmp_path)
    await spill.refile(harness.daemon._core)  # pyright: ignore[reportPrivateUsage]

    assert (spill.pending(), spill.undeliverable()) == (0, 1)


async def test_days_with_the_daemon_off_are_not_days_of_failing(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 20 failed tries in 100 minutes, then 8 days of downtime: the first failed
    # try at the next start must not shelve the mail.
    await _session(harness)
    monkeypatch.setattr(PickyRelay, "poison", "poison")
    stale = Held(
        message=_message("poison"),
        since=datetime.now(UTC) - timedelta(days=8),
        attempts=Spill.UNDELIVERABLE_ATTEMPTS,
    )
    _seed(tmp_path, _spilled("s-one").keeping((stale,)))

    spill = _spill(tmp_path)
    await spill.refile(harness.daemon._core)  # pyright: ignore[reportPrivateUsage]

    assert (spill.pending(), spill.undeliverable()) == (1, 0)


async def test_the_shelf_is_tried_again_while_the_daemon_runs(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _session(harness)
    monkeypatch.setattr(Spill, "RETRY_DELAYS", (0.02,))
    monkeypatch.setattr(Spill, "RETRY_INTERVAL", 0.02)
    monkeypatch.setattr(Spill, "UNSHELVE_INTERVAL", 0.0)
    SpillFile(*_files(tmp_path)).shelve(_spilled("s-one", "shelved"))
    spill = _spill(tmp_path)
    keeping = asyncio.create_task(
        spill.keep_filing(harness.daemon._core)  # pyright: ignore[reportPrivateUsage]
    )
    try:
        await until(lambda: spill.undeliverable() == 0 and spill.pending() == 0)
    finally:
        keeping.cancel()
        await asyncio.gather(keeping, return_exceptions=True)

    assert await _mailbox(harness) == ["shelved"]


class _SlowClose(PickyRelay):
    """A relay on a half-open network: it takes a while to close, or never does."""

    closing = 0.0
    closed: ClassVar[list[str]] = []

    async def close(self) -> None:
        await asyncio.sleep(type(self).closing)
        type(self).closed.append("closed")


async def test_closing_the_relays_is_bounded_at_stop(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.factory.relay_type = _SlowClose
    await harness.connect("s-kai", "kai")
    monkeypatch.setattr(_SlowClose, "closing", 60.0)
    monkeypatch.setattr(Handback, "POOL_CLOSE_SECONDS", 0.2)
    monkeypatch.setattr(Handback, "SETTLE_SECONDS", 0.05)

    async with asyncio.timeout(3.0):
        await Handback.close_relays(
            harness.daemon._core,  # pyright: ignore[reportPrivateUsage]
            _spill(tmp_path),
        )


async def test_relays_close_at_the_same_time_not_one_after_another(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.factory.relay_type = _SlowClose
    await harness.connect("s-kai", "kai")
    pool = harness.daemon._core.pool  # pyright: ignore[reportPrivateUsage]
    (target, *_) = pool._relays  # pyright: ignore[reportPrivateUsage]
    other = dataclasses.replace(target, repo_name="another-repo")
    pool._relays[other] = _SlowClose(harness.relays_dir / "second")  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(_SlowClose, "closing", 0.3)
    monkeypatch.setattr(_SlowClose, "closed", [])

    async with asyncio.timeout(
        0.5
    ):  # two closes of 0.3 s, one after the other, take 0.6
        await pool.close()

    assert _SlowClose.closed == ["closed", "closed"]


def test_the_inbox_stream_remembers_ids_longer_than_the_spill_retries() -> None:
    assert _INBOX_DUPLICATE_WINDOW_SECONDS >= 2 * Spill.RETRY_INTERVAL


async def test_the_shelf_is_retried_at_every_start(
    harness: Harness, tmp_path: Path
) -> None:
    await _session(harness)
    SpillFile(*_files(tmp_path)).shelve(_spilled("s-one", "shelved"))
    spill = _spill(tmp_path)
    assert spill.undeliverable() == 1

    await spill.resume(harness.daemon._core)  # pyright: ignore[reportPrivateUsage]

    assert (await _mailbox(harness), spill.undeliverable(), spill.pending()) == (
        ["shelved"],
        0,
        0,
    )


async def test_a_budget_that_ends_a_session_midway_records_what_it_filed(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _session(harness)
    monkeypatch.setattr(Spill, "REFILE_SECONDS", 0.2)
    monkeypatch.setattr(PickyRelay, "pace", 0.15)  # m1 is filed, m2 is cut off
    _seed(tmp_path, _spilled("s-one", "m1", "m2"))

    spill = _spill(tmp_path)
    await spill.refile(harness.daemon._core)  # pyright: ignore[reportPrivateUsage]

    (left,) = SpillFile(*_files(tmp_path)).read().values()
    assert (await _mailbox(harness), _bodies(left.messages)) == (["m1"], ["m2"])


async def test_mail_in_a_file_set_aside_is_counted_in_the_status(
    harness: Harness, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    await _session(harness)
    _files(tmp_path)[0].write_text("{not json")
    spill = _spill(tmp_path)

    with caplog.at_level(logging.ERROR, logger="biff.daemon.spill"):
        await spill.resume(harness.daemon._core)  # pyright: ignore[reportPrivateUsage]
    shown = spill.annotate(CommandResult(text="biff", json_data={"user": "kai"}))

    assert (
        shown.json_data,
        "set aside" in shown.text,
        "set aside" in caplog.text,
    ) == ({"user": "kai", "spill_files_set_aside": 1}, True, True)


async def test_losing_the_newest_snapshot_is_logged_before_the_older_one_is_used(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    primary, fallback = _files(tmp_path)
    SpillFile(primary, fallback).write({("s-one", False): _spilled("s-one", "x")})
    older = primary.read_bytes()
    primary.unlink()
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_bytes(older)  # the old snapshot, in the second place
    primary.write_text("{truncated")  # the newest, unreadable

    with caplog.at_level(logging.CRITICAL, logger="biff.daemon.spill_file"):
        (item,) = SpillFile(primary, fallback).read().values()

    assert ([m.body for m in item.messages], "may replay" in caplog.text) == (
        ["x"],
        True,
    )


async def test_a_batch_that_could_not_be_written_is_written_with_the_next_one(
    harness: Harness, tmp_path: Path
) -> None:
    session = await _session(harness)
    spill = _spill(tmp_path)
    spill.hold(session, [_message("a")])
    for directory in (tmp_path / "run", tmp_path):
        directory.mkdir(exist_ok=True)
        directory.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        spill.hold(session, [_message("b")])  # no place to write it
    finally:
        for directory in (tmp_path / "run", tmp_path):
            directory.chmod(stat.S_IRWXU)

    spill.hold(session, [_message("c")])

    (item,) = SpillFile(*_files(tmp_path)).read().values()
    assert _bodies(item.messages) == ["a", "b", "c"]


async def test_a_sealed_outbox_replaces_a_held_message_with_its_copy() -> None:
    outbox = Outbox(retains=True)
    original = _message("m1")
    outbox.merge([original])
    sunk: list[Message] = []
    outbox.seal(sunk.extend)
    copy = original.refiled()  # the relay took the copy a refile had landed

    outbox.merge([copy])

    assert (outbox.held(), sunk) == ([], [copy])


async def test_a_spilled_message_is_replaced_by_its_copy_not_kept_beside_it() -> None:
    original = _message("m1")
    entry = _spilled("s-one").with_mail([original])

    replaced = entry.with_mail([original.refiled()])

    assert [m.id for m in replaced.messages] == [original.refiled().id]


# -- silent failures ---------------------------------------------------------


def _two_snapshots(tmp_path: Path) -> tuple[Path, Path]:
    """Make a newer primary holding y, and a stale second copy holding x and y."""
    primary, fallback = _files(tmp_path)
    SpillFile(primary, fallback).write({("s-one", False): _spilled("s-one", "x", "y")})
    stale = primary.read_bytes()
    newer = SpillFile(primary, fallback)
    newer.read()
    newer.write({("s-one", False): _spilled("s-one", "y")})
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_bytes(stale)
    return primary, fallback


def _unreadable_by_io(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    real = pathlib.Path.read_text

    def failing(self: pathlib.Path, *args: str, **kwargs: str) -> str:
        if self == path:
            raise OSError(errno.EIO, "input/output error")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", failing)


def test_an_io_error_on_the_newest_snapshot_does_not_move_it_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    primary, fallback = _two_snapshots(tmp_path)
    _unreadable_by_io(monkeypatch, primary)

    with caplog.at_level(logging.CRITICAL, logger="biff.daemon.spill_file"):
        SpillFile(primary, fallback).read()

    assert (
        primary.exists(),
        list(tmp_path.glob("spill.unreadable-*.json")),
        "not in the live spill" in caplog.text,
    ) == (True, [], True)


def test_an_unreadable_only_snapshot_is_left_in_place_and_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    primary, fallback = _files(tmp_path)
    SpillFile(primary, fallback).write({("s-one", False): _spilled("s-one", "x")})
    _unreadable_by_io(monkeypatch, primary)

    with caplog.at_level(logging.CRITICAL, logger="biff.daemon.spill_file"):
        SpillFile(primary, fallback).read()

    assert (
        primary.exists(),
        list(tmp_path.glob("spill.unreadable-*.json")),
        "only snapshot" in caplog.text,
    ) == (True, [], True)


def test_an_emptied_spill_is_kept_while_the_older_file_cannot_be_dropped(
    tmp_path: Path,
) -> None:
    primary, fallback = _files(tmp_path)
    spill = SpillFile(primary, fallback)
    spill.write({("s-one", False): _spilled("s-one", "x")})
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_bytes(primary.read_bytes())  # a stale copy that stays
    fallback.parent.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        spill.read()
        spill.write({})  # everything was filed
    finally:
        fallback.parent.chmod(stat.S_IRWXU)

    assert SpillFile(primary, fallback).read() == {}


def test_entries_that_could_not_be_set_aside_are_not_lost_by_the_next_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary, fallback = _files(tmp_path)
    SpillFile(primary, fallback).write({("s-good", False): _spilled("s-good", "kept")})
    data = json.loads(primary.read_text())
    data["spilled"].append({"session_id": "s-bad-marker", "held": [{}]})
    primary.write_text(json.dumps(data))
    real = DurableFile.replace

    def refusing(directory: pathlib.Path, name: str, text: str) -> None:
        if ".corrupt-" in name:
            raise OSError(errno.ENOSPC, "no space left")
        real(directory, name, text)

    monkeypatch.setattr(DurableFile, "replace", staticmethod(refusing))
    spill = SpillFile(primary, fallback)
    kept = spill.read()
    spill.write(kept)  # the next write must not drop the entry it could not set aside

    saved = primary.read_text() + "".join(p.read_text() for p in tmp_path.glob("*"))
    assert "s-bad-marker" in saved


async def test_a_spill_that_cannot_be_serialized_never_raises_into_the_read(
    harness: Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = await _session(harness)

    def failing(_self: SpillFile, _entries: object) -> None:
        msg = "not serializable"
        raise TypeError(msg)

    monkeypatch.setattr(SpillFile, "write", failing)
    spill = _spill(tmp_path)

    with caplog.at_level(logging.CRITICAL, logger="biff.daemon.spill"):
        stored = spill.hold(session, [_message("m1")])

    assert (stored, '"body":"m1"' in caplog.text, spill.pending()) == (False, True, 1)


async def test_a_message_that_breaks_the_relay_does_not_stop_the_ones_after_it(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _session(harness)
    monkeypatch.setattr(PickyRelay, "crash", "bad")
    _seed(tmp_path, _spilled("s-one", "bad", "after"))

    spill = _spill(tmp_path)
    await spill.refile(harness.daemon._core)  # pyright: ignore[reportPrivateUsage]

    (left,) = SpillFile(*_files(tmp_path)).read().values()
    assert (await _mailbox(harness), _bodies(left.messages)) == (["after"], ["bad"])


async def test_a_try_is_counted_only_for_messages_that_were_tried(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _session(harness)
    monkeypatch.setattr(Spill, "REFILE_SECONDS", 0.2)
    monkeypatch.setattr(PickyRelay, "pace", 0.15)  # m1 filed, m2 cut off, m3 not tried
    _seed(tmp_path, _spilled("s-one", "m1", "m2", "m3"))

    spill = _spill(tmp_path)
    await spill.refile(harness.daemon._core)  # pyright: ignore[reportPrivateUsage]

    (left,) = SpillFile(*_files(tmp_path)).read().values()
    assert {h.message.body: h.attempts for h in left.held} == {"m2": 1, "m3": 0}


async def test_the_stop_says_which_sessions_it_could_not_spill(
    harness: Harness, tmp_path: Path
) -> None:
    session = await _session(harness)
    session.outbox.merge([_message("m1")])
    spill = _spill(tmp_path)
    for directory in (tmp_path / "run", tmp_path):
        directory.mkdir(exist_ok=True)
        directory.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        failed = Handback._spill_all([session], spill)  # pyright: ignore[reportPrivateUsage]
    finally:
        for directory in (tmp_path / "run", tmp_path):
            directory.chmod(stat.S_IRWXU)

    assert failed == {session.session_id}


async def test_a_start_that_fails_leaves_no_retry_task_behind(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing(_self: object) -> None:
        msg = "cannot restore"
        raise RuntimeError(msg)

    monkeypatch.setattr("biff.daemon.session_restore.SessionRestore.run", failing)
    factory = CountingFactory()
    daemon = BiffDaemon(
        run_dir=RunDir(tmp_path / "run-fail"),
        port=0,
        sessions=harness.sessions,
        resolver=FakeResolver(harness.relays_dir),
        relay_factory=factory,
    )

    def retrying() -> set[asyncio.Task[object]]:
        return {
            t
            for t in asyncio.all_tasks()
            if t.get_name() == "spill retry" and not t.done()
        }

    before = retrying()  # the harness daemon's own
    with pytest.raises(RuntimeError, match="cannot restore"):
        await daemon.start()
    await asyncio.sleep(0)

    assert retrying() - before == set()


def test_a_status_call_does_not_set_a_corrupt_shelf_aside(tmp_path: Path) -> None:
    primary, fallback = _files(tmp_path)
    shelf = primary.with_suffix(".undeliverable")
    shelf.write_text("{corrupt")

    Spill(primary, fallback).undeliverable()

    assert (shelf.exists(), list(tmp_path.glob("spill.unreadable-*"))) == (True, [])
