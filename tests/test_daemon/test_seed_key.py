"""The seed key survives a crash, a bad file and a strange umask, and says so."""

from __future__ import annotations

import logging
import os
import stat
from typing import TYPE_CHECKING

import pytest

from biff.daemon.seed_key import SeedKey

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "relay-seed.key"


def test_the_key_is_private_and_stable(path: Path) -> None:
    first = SeedKey(path).load()
    assert (len(first), stat.S_IMODE(path.stat().st_mode)) == (32, 0o600)
    assert SeedKey(path).load() == first


def test_no_temporary_file_is_left_beside_the_key(path: Path) -> None:
    SeedKey(path).load()
    assert [p.name for p in path.parent.iterdir()] == ["relay-seed.key"]


def test_an_empty_key_file_is_replaced_not_fatal(
    path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path.write_bytes(b"")
    path.chmod(0o600)
    key = SeedKey(path).load()
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert (len(key), any(str(path) in m for m in warned)) == (32, True)


def test_a_key_file_open_to_others_is_replaced_not_fatal(path: Path) -> None:
    first = SeedKey(path).load()
    path.chmod(0o644)
    second = SeedKey(path).load()
    assert (second != first, stat.S_IMODE(path.stat().st_mode)) == (True, 0o600)


def test_a_symlink_is_replaced_and_its_target_left_alone(
    path: Path, tmp_path: Path
) -> None:
    target = tmp_path / "elsewhere"
    target.write_bytes(b"k" * 32)
    target.chmod(0o600)
    path.symlink_to(target)
    key = SeedKey(path).load()
    assert (path.is_symlink(), target.read_bytes() == b"k" * 32, len(key)) == (
        False,
        True,
        32,
    )


def test_a_read_only_key_is_accepted_as_it_is(path: Path) -> None:
    first = SeedKey(path).load()
    path.chmod(0o400)
    assert SeedKey(path).load() == first


def test_a_strict_umask_still_leaves_a_usable_key(path: Path) -> None:
    before = os.umask(0o277)
    try:
        key = SeedKey(path).load()
    finally:
        os.umask(before)
    assert (len(key), stat.S_IMODE(path.stat().st_mode) & 0o077) == (32, 0)


def test_a_key_made_beside_an_existing_seed_record_is_warned_about(
    path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (path.parent / "relay-seed").write_text("none\n")
    SeedKey(path).load()
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(str(path) in m for m in warned)


def test_the_first_key_is_made_without_a_warning(
    path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    SeedKey(path).load()
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
