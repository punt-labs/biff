"""The seed key survives a crash, a bad file and a strange umask, and says so."""

from __future__ import annotations

import logging
import os
import stat
import threading
import time
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
    left = {p.name for p in path.parent.iterdir()} - {"relay-seed.key.lock"}
    assert left == {"relay-seed.key"}


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


def test_two_starts_at_once_both_end_with_the_key_on_disk(path: Path) -> None:
    original = SeedKey._read  # pyright: ignore[reportPrivateUsage]
    a_done = threading.Event()
    paused: list[bool] = []
    keys: dict[str, bytes] = {}

    def read(self: SeedKey) -> bytes | None:
        found = original(self)
        if threading.current_thread().name == "B" and not paused:
            paused.append(True)
            a_done.wait(timeout=1)  # A cannot run until B lets go of the lock
        return found

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SeedKey, "_read", read)
        b = threading.Thread(
            target=lambda: keys.update(B=SeedKey(path).load()), name="B"
        )
        b.start()
        while not paused:
            time.sleep(0.01)
        keys["A"] = SeedKey(path).load()
        a_done.set()
        b.join(timeout=10)
    assert (keys["A"] == path.read_bytes(), keys["B"] == path.read_bytes()) == (
        True,
        True,
    )


def test_a_directory_at_the_key_path_is_replaced_with_a_warning(
    path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path.mkdir()
    key = SeedKey(path).load()
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert (path.is_file(), len(key), any(str(path) in m for m in warned)) == (
        True,
        32,
        True,
    )


def test_a_bad_key_file_is_moved_aside_not_deleted(path: Path) -> None:
    path.write_bytes(b"short")
    path.chmod(0o600)
    SeedKey(path).load()
    assert [p.read_bytes() for p in _aside(path)] == [b"short"]


def _aside(path: Path) -> list[Path]:
    return sorted(p for p in path.parent.iterdir() if ".bad" in p.name)


def test_two_starts_judging_a_bad_key_at_once_end_with_the_key_on_disk(
    path: Path,
) -> None:
    path.write_bytes(b"short")
    path.chmod(0o600)
    original = SeedKey._set_aside  # pyright: ignore[reportPrivateUsage]
    inside = threading.Event()
    release = threading.Event()
    keys: dict[str, bytes] = {}

    def set_aside(self: SeedKey, why: str, mode: int = 0) -> None:
        if threading.current_thread().name == "B":
            inside.set()  # B has judged the key bad and is about to move it
            release.wait(timeout=10)
        original(self, why, mode)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SeedKey, "_set_aside", set_aside)
        b = threading.Thread(
            target=lambda: keys.update(B=SeedKey(path).load()), name="B"
        )
        a = threading.Thread(
            target=lambda: keys.update(A=SeedKey(path).load()), name="A"
        )
        b.start()
        assert inside.wait(timeout=10)
        a.start()
        time.sleep(0.5)  # A runs now if nothing holds it back
        release.set()
        b.join(timeout=10)
        a.join(timeout=10)
    disk = path.read_bytes()
    assert (keys["A"] == disk, keys["B"] == disk, len(_aside(path))) == (True, True, 1)


def test_a_key_moved_aside_is_private_whatever_it_was(path: Path) -> None:
    path.write_bytes(b"old key material that is open")
    path.chmod(0o644)
    SeedKey(path).load()
    (aside,) = _aside(path)
    assert stat.S_IMODE(aside.stat().st_mode) == 0o600


def test_many_bad_keys_leave_one_file_aside(path: Path) -> None:
    for _ in range(5):
        path.write_bytes(b"short")
        path.chmod(0o600)
        SeedKey(path).load()
    assert [p.name for p in _aside(path)] == ["relay-seed.key.bad"]
