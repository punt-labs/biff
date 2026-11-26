"""The daemon's run directory: token, port file, modes."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from biff.run_dir import DEFAULT_PORT, RunDir


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_ensure_creates_the_directory_private(tmp_path: Path) -> None:
    run = RunDir(tmp_path / "run")
    run.ensure()
    assert _mode(run.path) == 0o700


def test_token_is_created_once_at_mode_0600(tmp_path: Path) -> None:
    run = RunDir(tmp_path / "run")
    run.ensure_token()
    assert _mode(run.token_path) == 0o600


def test_token_has_enough_entropy(tmp_path: Path) -> None:
    assert len(RunDir(tmp_path / "run").ensure_token()) >= 32


def test_token_is_stable_across_calls(tmp_path: Path) -> None:
    run = RunDir(tmp_path / "run")
    assert run.ensure_token() == RunDir(tmp_path / "run").ensure_token()


def test_read_token_returns_the_created_token(tmp_path: Path) -> None:
    run = RunDir(tmp_path / "run")
    created = run.ensure_token()
    assert run.read_token() == created


def test_read_token_without_a_daemon_names_the_file(tmp_path: Path) -> None:
    run = RunDir(tmp_path / "run")
    with pytest.raises(FileNotFoundError, match=r"serve\.token"):
        run.read_token()


def test_loosened_token_mode_is_refused(tmp_path: Path) -> None:
    run = RunDir(tmp_path / "run")
    run.ensure_token()
    run.token_path.chmod(0o644)
    with pytest.raises(PermissionError, match="0600"):
        run.read_token()


def test_port_file_round_trips_and_is_removed(tmp_path: Path) -> None:
    run = RunDir(tmp_path / "run")
    run.write_port(51234)
    assert run.read_port() == 51234
    run.remove_port()
    assert not run.port_path.exists()


def test_absent_port_file_means_the_default_port(tmp_path: Path) -> None:
    assert RunDir(tmp_path / "run").read_port() == DEFAULT_PORT == 8424


def test_default_run_dir_is_under_the_home_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert RunDir.default().path == tmp_path / ".punt-labs" / "biff" / "run"
