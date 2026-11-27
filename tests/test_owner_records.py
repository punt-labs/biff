"""Owner public keys are recorded in the clear, so reading one asks for nothing."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from biff.key_vault import VaultError
from biff.owner_records import OwnerRecords

PUBLIC = "ab" * 32


def test_a_recorded_key_reads_back(tmp_path: Path) -> None:
    records = OwnerRecords(tmp_path / "owner")
    records.put("jfreeman", PUBLIC)
    assert records.get("jfreeman") == PUBLIC


def test_an_unrecorded_owner_is_none(tmp_path: Path) -> None:
    assert OwnerRecords(tmp_path).get("nobody") is None


def test_has_reports_a_record(tmp_path: Path) -> None:
    records = OwnerRecords(tmp_path)
    before = records.has("jfreeman")
    records.put("jfreeman", PUBLIC)
    assert (before, records.has("jfreeman")) == (False, True)


def test_a_record_is_private_to_its_user(tmp_path: Path) -> None:
    directory = tmp_path / "owner"
    OwnerRecords(directory).put("jfreeman", PUBLIC)
    assert stat.S_IMODE((directory / "jfreeman.pub").stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_a_second_record_replaces_the_first(tmp_path: Path) -> None:
    records = OwnerRecords(tmp_path)
    records.put("jfreeman", PUBLIC)
    records.put("jfreeman", "cd" * 32)
    assert records.get("jfreeman") == "cd" * 32


def test_a_handle_that_escapes_the_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(VaultError, match="invalid account"):
        OwnerRecords(tmp_path).put("../evil", PUBLIC)


def test_an_unreadable_record_is_a_vault_error(tmp_path: Path) -> None:
    blocked = tmp_path / "owner"
    blocked.write_text("a file, not a directory")
    with pytest.raises(VaultError, match="jfreeman"):
        OwnerRecords(blocked).get("jfreeman")


def test_a_write_failure_is_a_vault_error_and_leaves_no_temp_file(
    tmp_path: Path,
) -> None:
    blocked = tmp_path / "owner"
    blocked.write_text("a file, not a directory")
    with pytest.raises(VaultError, match="jfreeman"):
        OwnerRecords(blocked).put("jfreeman", PUBLIC)
    assert blocked.read_text() == "a file, not a directory"
