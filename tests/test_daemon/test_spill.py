"""The spill: mail a stopping daemon could not give back, kept for the next start."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING

import pytest

from biff.daemon.durable_file import DurableFile
from biff.daemon.outbox import Outbox
from biff.daemon.spill import Spill
from biff.models import Message
from tests.test_daemon.consuming import ConsumingRelay

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from biff.daemon.sessions import Session
    from tests.test_daemon.conftest import Harness


def _message(body: str) -> Message:
    return Message(from_user="eric", to_user="kai", body=body)


async def _session(harness: Harness) -> Session:
    harness.factory.relay_type = ConsumingRelay
    await harness.connect("s-kai", "kai")
    (session,) = harness.daemon._core.sessions.snapshot().values()  # pyright: ignore[reportPrivateUsage]
    return session


def _bodies(messages: Sequence[Message]) -> list[str]:
    return sorted(m.body for m in messages)


async def test_a_spill_is_one_private_file_and_a_message_is_in_it_once(
    harness: Harness, tmp_path: Path
) -> None:
    session = await _session(harness)
    spill = Spill(tmp_path / "spill.json")
    mail = _message("m1")

    spill.hold(session, [mail])
    spill.hold(session, [mail])  # held twice, kept once

    text = (tmp_path / "spill.json").read_text()
    assert (
        stat.S_IMODE((tmp_path / "spill.json").stat().st_mode),
        text.count('"body": "m1"'),
    ) == (0o600, 1)


async def test_nothing_to_spill_writes_no_file(
    harness: Harness, tmp_path: Path
) -> None:
    session = await _session(harness)

    Spill(tmp_path / "spill.json").hold(session, [])

    assert not (tmp_path / "spill.json").exists()


async def test_a_later_spill_adds_to_what_an_earlier_stop_left(
    harness: Harness, tmp_path: Path
) -> None:
    session = await _session(harness)
    Spill(tmp_path / "spill.json").hold(session, [_message("old")])

    Spill(tmp_path / "spill.json").hold(session, [_message("new")])

    text = (tmp_path / "spill.json").read_text()
    assert ('"body": "old"' in text, '"body": "new"' in text) == (True, True)


async def test_an_unreadable_spill_is_set_aside_not_overwritten(
    harness: Harness, tmp_path: Path
) -> None:
    session = await _session(harness)
    (tmp_path / "spill.json").write_text("{not json")

    Spill(tmp_path / "spill.json").hold(session, [_message("m1")])

    assert (tmp_path / "spill.corrupt").read_text() == "{not json"
    assert '"body": "m1"' in (tmp_path / "spill.json").read_text()


async def test_a_spill_refiled_with_no_relay_trouble_leaves_no_file(
    harness: Harness, tmp_path: Path
) -> None:
    session = await _session(harness)
    spill = Spill(tmp_path / "spill.json")
    spill.hold(session, [_message("m1")])

    await spill.refile(harness.daemon._core)  # pyright: ignore[reportPrivateUsage]

    assert not (tmp_path / "spill.json").exists()


async def test_a_sealed_outbox_hands_what_it_fetches_to_the_sink() -> None:
    outbox = Outbox(retains=True)
    early, late = _message("early"), _message("late")
    outbox.merge([early])
    spilled: list[Message] = []

    outbox.seal(spilled.extend)
    served = outbox.merge([late])

    assert (_bodies(spilled), _bodies(outbox.held()), _bodies(served)) == (
        ["late"],
        ["early"],
        ["late"],
    )


async def test_a_sealed_outbox_of_a_relay_that_keeps_mail_spills_nothing() -> None:
    outbox = Outbox(retains=False)
    spilled: list[Message] = []

    outbox.seal(spilled.extend)
    outbox.merge([_message("m1")])

    assert spilled == []


def test_a_durable_file_is_replaced_whole_and_private(tmp_path: Path) -> None:
    path = tmp_path / "private" / "file.json"

    DurableFile.replace(path.parent, path.name, "one")
    DurableFile.replace(path.parent, path.name, "two")

    assert (
        path.read_text(),
        stat.S_IMODE(path.stat().st_mode),
        stat.S_IMODE(path.parent.stat().st_mode),
        [p.name for p in path.parent.iterdir()],
    ) == ("two", 0o600, 0o700, ["file.json"])


def test_a_failed_replace_leaves_the_old_file_and_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "file.json"
    DurableFile.replace(path.parent, path.name, "old")

    def failing(_fd: int) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr("os.fsync", failing)
    with pytest.raises(OSError, match="disk full"):
        DurableFile.replace(path.parent, path.name, "new")

    assert (path.read_text(), [p.name for p in tmp_path.iterdir()]) == (
        "old",
        ["file.json"],
    )
