"""The kept harness sessions: one small private file each, unreadable ones skipped."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING

from biff.daemon.harness_records import HarnessRecords
from biff.daemon.session_key import SessionKey
from biff.frame_client import Hello
from biff.session_id import CLAUDE_CODE_ROLE

if TYPE_CHECKING:
    from pathlib import Path


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
    records.save(_hello())

    (back,) = records.load()

    assert (back.client, back.session_id, back.main_agent_id, back.cwd, back.user) == (
        "mcp",
        "s-one",
        "a1",
        "/work/repo",
        "kai",
    )


def test_a_record_is_private_to_its_owner(tmp_path: Path) -> None:
    records = HarnessRecords(tmp_path / "records")
    records.save(_hello())

    (path,) = (tmp_path / "records").glob("*.json")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_saving_a_session_again_replaces_its_record(tmp_path: Path) -> None:
    records = HarnessRecords(tmp_path / "records")
    records.save(_hello(cwd="/work/one"))
    records.save(_hello(cwd="/work/two"))

    assert [h.cwd for h in records.load()] == ["/work/two"]


def test_a_removed_session_is_gone_and_removing_twice_is_fine(tmp_path: Path) -> None:
    records = HarnessRecords(tmp_path / "records")
    records.save(_hello())
    key = SessionKey("s-one", CLAUDE_CODE_ROLE)

    records.remove(key)
    records.remove(key)

    assert records.load() == ()


def test_an_unreadable_record_is_skipped_not_fatal(tmp_path: Path) -> None:
    records = HarnessRecords(tmp_path / "records")
    records.save(_hello("s-good"))
    (tmp_path / "records" / "s-bad.json").write_text("{not json")

    assert [h.session_id for h in records.load()] == ["s-good"]


def test_no_records_directory_is_no_sessions(tmp_path: Path) -> None:
    assert HarnessRecords(tmp_path / "never-made").load() == ()
