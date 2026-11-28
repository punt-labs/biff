"""biffd remembers which relay settings it was last started with, names only."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING

from biff.daemon.seed_record import SeedRecord
from biff.relay_seed import RelaySeed

if TYPE_CHECKING:
    from pathlib import Path

_SEEDED = {"BIFF_RELAY_URL": "nats://relay.invalid:4222", "BIFF_RELAY_TOKEN": "tok-A"}


def test_the_first_start_has_nothing_to_compare_with(tmp_path: Path) -> None:
    assert SeedRecord(tmp_path / "relay-seed").record(RelaySeed.of(_SEEDED)) == ""


def test_the_file_holds_names_only_and_is_private(tmp_path: Path) -> None:
    path = tmp_path / "relay-seed"
    SeedRecord(path).record(RelaySeed.of(_SEEDED))
    assert path.read_text() == "BIFF_RELAY_TOKEN, BIFF_RELAY_URL\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_start_with_the_same_names_says_nothing(tmp_path: Path) -> None:
    record = SeedRecord(tmp_path / "relay-seed")
    record.record(RelaySeed.of(_SEEDED))
    assert record.record(RelaySeed.of({**_SEEDED, "BIFF_RELAY_TOKEN": "tok-B"})) == ""


def test_a_start_without_the_settings_says_what_changed(tmp_path: Path) -> None:
    record = SeedRecord(tmp_path / "relay-seed")
    record.record(RelaySeed.of(_SEEDED))
    note = record.record(RelaySeed.of({}))
    assert (
        "BIFF_RELAY_TOKEN, BIFF_RELAY_URL" in note,
        "none" in note,
        "tok-A" in note,
    ) == (True, True, False)


def test_the_change_is_reported_once(tmp_path: Path) -> None:
    record = SeedRecord(tmp_path / "relay-seed")
    record.record(RelaySeed.of(_SEEDED))
    record.record(RelaySeed.of({}))
    assert record.record(RelaySeed.of({})) == ""
