"""The kept harness sessions: small, private, durable, and tolerant of a bad file."""

from __future__ import annotations

import logging
import os
import stat
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from biff.daemon.harness_records import (
    HarnessRecords,
    _Stored,  # pyright: ignore[reportPrivateUsage]
    _StoredHello,  # pyright: ignore[reportPrivateUsage]
)
from biff.daemon.refusal import RefusalError
from biff.daemon.session_key import SessionKey
from biff.frame_client import Hello
from biff.session_id import CLAUDE_CODE_ROLE

if TYPE_CHECKING:
    from pathlib import Path

_KEY = SessionKey("s-one", CLAUDE_CODE_ROLE)


def _hello(session_id: str = "s-one", cwd: str = "/work/repo") -> Hello:
    return Hello(
        mac="f" * 64,
        client="mcp",
        session_id=session_id,
        main_agent_id="a1",
        cwd=cwd,
        user="kai",
    )


def test_a_kept_session_comes_back_without_its_proof(tmp_path: Path) -> None:
    records = HarnessRecords(tmp_path / "records")
    records.keep(_hello())

    (back,) = records.load()

    assert (
        back.hello.client,
        back.hello.session_id,
        back.hello.main_agent_id,
        back.hello.cwd,
        back.hello.user,
        back.dormant_since,
        back.failed_starts,
    ) == ("mcp", "s-one", "a1", "/work/repo", "kai", None, 0)


def test_a_session_is_marked_dormant_and_attached_again(tmp_path: Path) -> None:
    records = HarnessRecords(tmp_path / "records")
    records.keep(_hello())
    left = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    records.mark(_KEY, left)
    marked = records.load()[0].dormant_since
    records.mark(_KEY, None)

    assert (marked, records.load()[0].dormant_since) == (left, None)


def test_failed_starts_are_counted_and_kept(tmp_path: Path) -> None:
    records = HarnessRecords(tmp_path / "records")
    records.keep(_hello())

    counts = (records.failed(_KEY), records.failed(_KEY))

    assert (counts, records.load()[0].failed_starts) == ((1, 2), 2)


def test_a_record_is_private_to_its_owner(tmp_path: Path) -> None:
    records = HarnessRecords(tmp_path / "records")
    records.keep(_hello())

    (path,) = (tmp_path / "records").glob("*.json")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_keeping_a_session_again_replaces_its_record(tmp_path: Path) -> None:
    records = HarnessRecords(tmp_path / "records")
    records.keep(_hello(cwd="/work/one"))
    records.keep(_hello(cwd="/work/two"))

    assert [k.hello.cwd for k in records.load()] == ["/work/two"]


def test_a_removed_session_is_gone_and_removing_twice_is_fine(tmp_path: Path) -> None:
    records = HarnessRecords(tmp_path / "records")
    records.keep(_hello())

    records.remove(_KEY)
    records.remove(_KEY)

    assert records.load() == ()


def test_a_record_that_cannot_be_deleted_is_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "records"
    records = HarnessRecords(root)
    records.keep(_hello())
    root.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with caplog.at_level(logging.ERROR, logger="biff.daemon.harness_records"):
            records.remove(_KEY)
    finally:
        root.chmod(stat.S_IRWXU)

    assert (
        "could not delete the record of session s-one" in caplog.text,
        len(records.load()),
    ) == (
        True,
        1,
    )


@pytest.mark.parametrize("text", ["[]", "null", '"text"', "3", "{not json", "{}"])
def test_a_file_that_is_not_a_record_is_skipped_not_fatal(
    tmp_path: Path, text: str
) -> None:
    records = HarnessRecords(tmp_path / "records")
    records.keep(_hello("s-good"))
    (tmp_path / "records" / "s-bad.json").write_text(text)

    assert [k.hello.session_id for k in records.load()] == ["s-good"]


def test_no_records_directory_is_no_sessions(tmp_path: Path) -> None:
    assert HarnessRecords(tmp_path / "never-made").load() == ()


def test_a_record_that_cannot_be_written_is_a_refusal_naming_it(
    tmp_path: Path,
) -> None:
    blocker = tmp_path / "records"
    blocker.write_text("a file where the directory should be")

    with pytest.raises(RefusalError, match="cannot keep the session record"):
        HarnessRecords(blocker).keep(_hello())


def test_a_record_is_synced_to_disk_with_its_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[bool] = []
    real = os.fsync

    def recording(fd: int) -> None:
        synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        real(fd)

    monkeypatch.setattr(os, "fsync", recording)
    HarnessRecords(tmp_path / "records").keep(_hello())

    assert synced == [False, True]  # the file's data, then the directory entry


def test_what_a_record_holds_cannot_be_changed_in_memory() -> None:
    stored = _Stored(
        hello=_StoredHello(
            client="mcp", session_id="s-one", main_agent_id="a1", cwd="/w", user="kai"
        )
    )

    field = "cwd"
    with pytest.raises(ValidationError):
        setattr(stored.hello, field, "/elsewhere")


def test_a_reattach_records_the_run_it_claims_and_keeps_the_run_of_a_silent_client(
    tmp_path: Path,
) -> None:
    records = HarnessRecords(tmp_path / "records")
    records.keep(_hello())
    records.mark(_KEY, datetime(2026, 1, 1, tzinfo=UTC))

    records.attached(_KEY, "a2")
    after_run = records.load()[0]
    records.attached(_KEY, "")

    assert (after_run.hello.main_agent_id, after_run.dormant_since) == ("a2", None)
    assert records.load()[0].hello.main_agent_id == "a2"
