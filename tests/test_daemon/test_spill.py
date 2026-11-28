"""The spill is the only copy of mail the relay gave up: no error path may lose it."""

from __future__ import annotations

import asyncio
import json
import logging
import stat
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from biff.commands._result import CommandResult
from biff.daemon.durable_file import DurableFile, UnsyncedError
from biff.daemon.outbox import Outbox
from biff.daemon.spill import Spill
from biff.daemon.spill_file import Spilled, SpillFile
from biff.models import Message
from biff.relay import LocalRelay
from tests.test_daemon.consuming import ConsumingRelay

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from biff.daemon.sessions import Session
    from tests.test_daemon.conftest import Harness


class PickyRelay(ConsumingRelay):
    """A consuming relay that refuses one body and can be slow."""

    poison = ""
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
        await asyncio.sleep(type(self).pace)
        await super().deliver(message, sender_key=sender_key, target_repo=target_repo)


def _message(body: str) -> Message:
    return Message(from_user="eric", to_user="kai", body=body)


def _spilled(session_id: str, *bodies: str, since: datetime | None = None) -> Spilled:
    spilled = Spilled(
        session_id=session_id,
        agent=False,
        cwd="/work/repo",
        user="kai",
        messages=tuple(_message(b) for b in bodies),
    )
    return spilled if since is None else spilled.model_copy(update={"since": since})


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
    data["spilled"].append({"session_id": 3, "messages": [{}, {}]})
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

    assert (spill.pending(), "LOST unless found here" in caplog.text) == (1, True)


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


async def test_mail_that_has_failed_for_a_week_is_shelved_not_retried_forever(
    harness: Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _session(harness)
    monkeypatch.setattr(PickyRelay, "poison", "poison")
    old = datetime.now(UTC) - timedelta(days=8)
    _seed(tmp_path, _spilled("s-one", "poison", since=old))

    spill = _spill(tmp_path)
    with caplog.at_level(logging.ERROR, logger="biff.daemon.spill"):
        await spill.refile(harness.daemon._core)  # pyright: ignore[reportPrivateUsage]

    shelf = json.loads(_files(tmp_path)[0].with_suffix(".undeliverable").read_text())
    assert (
        spill.pending(),
        [e["session_id"] for e in shelf["spilled"]],
        "8 days" in caplog.text,
    ) == (0, ["s-one"], True)


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
