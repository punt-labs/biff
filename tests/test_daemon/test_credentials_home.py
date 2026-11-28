"""Seeded credentials go only to the seeded relay, whatever names the file."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from biff.models import RelayAuth
from biff.relay_seed import RelayConflictError, RelaySeed

_URL = {"BIFF_RELAY_URL": "nats://relay.invalid:4222"}
_ELSEWHERE = "nats://elsewhere:4222"


def _creds(directory: Path, name: str = "real.creds") -> Path:
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_text("x")
    return path


def test_a_symlink_to_the_seeded_file_is_the_seeded_credential(tmp_path: Path) -> None:
    real = _creds(tmp_path)
    link = tmp_path / "link.creds"
    link.symlink_to(real)
    seed = RelaySeed.of({**_URL, "BIFF_RELAY_USER_CREDENTIALS": str(link)})
    with pytest.raises(RelayConflictError, match="BIFF_RELAY_URL"):
        seed.require_credentials_home(_ELSEWHERE, RelayAuth(user_credentials=str(real)))


def test_a_relative_path_to_the_seeded_file_is_the_seeded_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = _creds(tmp_path)
    seed = RelaySeed.of({**_URL, "BIFF_RELAY_USER_CREDENTIALS": str(real)})
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RelayConflictError, match="BIFF_RELAY_URL"):
        seed.require_credentials_home(
            _ELSEWHERE, RelayAuth(user_credentials="./real.creds")
        )


def test_a_dotdot_path_to_the_seeded_file_is_the_seeded_credential(
    tmp_path: Path,
) -> None:
    real = _creds(tmp_path / "a")
    seed = RelaySeed.of({**_URL, "BIFF_RELAY_NKEYS_SEED": str(real)})
    spelled = f"{tmp_path}/b/../a/real.creds"
    with pytest.raises(RelayConflictError, match="BIFF_RELAY_URL"):
        seed.require_credentials_home(_ELSEWHERE, RelayAuth(nkeys_seed=spelled))


def test_another_file_of_the_same_name_is_not_the_seeded_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _creds(tmp_path / "a")
    _creds(tmp_path / "b")
    seed = RelaySeed.of({**_URL, "BIFF_RELAY_USER_CREDENTIALS": str(seeded)})
    monkeypatch.chdir(tmp_path / "b")
    seed.require_credentials_home(_ELSEWHERE, RelayAuth(user_credentials="real.creds"))


def test_a_client_spelling_the_seeded_file_another_way_equals_the_seed(
    tmp_path: Path,
) -> None:
    real = _creds(tmp_path)
    link = tmp_path / "link.creds"
    link.symlink_to(real)
    seeded = RelaySeed.of({**_URL, "BIFF_RELAY_USER_CREDENTIALS": str(real)})
    client = RelaySeed.of({**_URL, "BIFF_RELAY_USER_CREDENTIALS": str(link)})
    assert client.differing_from(seeded) == ()


def _daemon_seed(creds: Path) -> RelaySeed:
    return RelaySeed.for_daemon({**_URL, "BIFF_RELAY_USER_CREDENTIALS": str(creds)})


def test_a_symlink_made_and_re_pointed_after_start_is_judged_as_it_is_now(
    tmp_path: Path,
) -> None:
    seeded = _creds(tmp_path / "a")
    harmless = _creds(tmp_path / "b", "other.creds")
    seed = _daemon_seed(seeded)
    link = tmp_path / "late.creds"
    link.symlink_to(harmless)
    seed.require_credentials_home(_ELSEWHERE, RelayAuth(user_credentials=str(link)))
    link.unlink()
    link.symlink_to(seeded)
    with pytest.raises(RelayConflictError, match="BIFF_RELAY_URL"):
        seed.require_credentials_home(_ELSEWHERE, RelayAuth(user_credentials=str(link)))


def test_a_hardlink_to_the_seeded_file_is_the_seeded_credential(
    tmp_path: Path,
) -> None:
    seeded = _creds(tmp_path / "a")
    seed = _daemon_seed(seeded)
    hard = tmp_path / "hard.creds"
    os.link(seeded, hard)
    with pytest.raises(RelayConflictError, match="BIFF_RELAY_URL") as refused:
        seed.require_credentials_home(_ELSEWHERE, RelayAuth(user_credentials=str(hard)))
    assert str(seeded) not in str(refused.value)


def test_a_credential_file_that_cannot_be_read_is_not_the_seeded_one(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    seed = _daemon_seed(_creds(tmp_path / "a"))
    missing = tmp_path / "missing.creds"
    seed.require_credentials_home(_ELSEWHERE, RelayAuth(user_credentials=str(missing)))
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert [
        "BIFF_RELAY_USER_CREDENTIALS" in m and str(missing) not in m for m in warned
    ] == [True]
