"""The daemon's run directory: token, port file, modes."""

from __future__ import annotations

import os
import stat
import threading
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


def test_a_run_directory_others_can_enter_is_refused(tmp_path: Path) -> None:
    run = RunDir(tmp_path / "run")
    run.ensure_token()
    run.path.chmod(0o755)
    with pytest.raises(PermissionError, match=r"chmod 700"):
        run.read_token()


def test_a_loose_run_directory_is_refused_before_a_token_is_created(
    tmp_path: Path,
) -> None:
    (tmp_path / "run").mkdir(mode=0o755)
    (tmp_path / "run").chmod(0o755)
    with pytest.raises(PermissionError, match=r"chmod 700"):
        RunDir(tmp_path / "run").ensure_token()


def test_a_token_file_owned_by_someone_else_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = RunDir(tmp_path / "run")
    run.ensure_token()
    monkeypatch.setattr("os.getuid", lambda: run.token_path.stat().st_uid + 1)
    with pytest.raises(PermissionError, match="owned by"):
        run.read_token()


def test_a_run_directory_owned_by_someone_else_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = RunDir(tmp_path / "run")
    run.ensure_token()
    monkeypatch.setattr("os.getuid", lambda: run.path.stat().st_uid + 1)
    with pytest.raises(PermissionError, match="owned by"):
        run.read_token()


def test_the_refusal_names_the_repair(tmp_path: Path) -> None:
    run = RunDir(tmp_path / "run")
    run.ensure_token()
    run.token_path.chmod(0o640)
    with pytest.raises(PermissionError, match=r"chmod 600"):
        run.read_token()


def _plant_token(run: RunDir, text: str) -> None:
    run.ensure()
    run.token_path.write_text(text)
    run.token_path.chmod(0o600)


@pytest.mark.parametrize("text", ["", "\n", "short", "x" * 42, "x" * 44, "!" * 43])
def test_a_token_of_the_wrong_shape_is_refused(tmp_path: Path, text: str) -> None:
    run = RunDir(tmp_path / "run")
    _plant_token(run, text)
    with pytest.raises(ValueError, match=r"serve\.token"):
        run.read_token()


def test_an_empty_token_file_is_never_returned_as_a_token(tmp_path: Path) -> None:
    """An empty key would make every HMAC computable by anyone."""
    run = RunDir(tmp_path / "run")
    _plant_token(run, "")
    with pytest.raises(ValueError, match="remove it"):
        run.ensure_token()


def test_a_valid_token_with_a_trailing_newline_is_accepted(tmp_path: Path) -> None:
    run = RunDir(tmp_path / "run")
    token = run.ensure_token()
    run.token_path.write_text(token + "\n")
    assert run.read_token() == token


def test_concurrent_creators_agree_on_one_complete_token(tmp_path: Path) -> None:
    run = RunDir(tmp_path / "run")
    run.ensure()
    seen: list[str] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def create() -> None:
        barrier.wait()
        try:
            seen.append(RunDir(tmp_path / "run").ensure_token())
        except BaseException as exc:  # noqa: BLE001 - reported by the assertion
            errors.append(exc)

    threads = [threading.Thread(target=create) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(set(seen)) == 1
    assert len(seen[0]) == 43


def test_creation_leaves_no_temporary_file(tmp_path: Path) -> None:
    run = RunDir(tmp_path / "run")
    run.ensure_token()
    assert sorted(path.name for path in run.path.iterdir()) == ["serve.token"]


def test_the_token_is_never_visible_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader at any moment before the link sees no token, not an empty one."""
    run = RunDir(tmp_path / "run")
    states: list[bool] = []
    real_link = os.link

    def spying_link(src: str | Path, dst: str | Path) -> None:
        states.append(run.token_path.exists())
        real_link(src, dst)

    monkeypatch.setattr(os, "link", spying_link)
    run.ensure_token()
    assert states == [False]


def test_write_port_replaces_the_file_atomically(tmp_path: Path) -> None:
    run = RunDir(tmp_path / "run")
    run.write_port(1111)
    run.write_port(2222)
    assert run.read_port() == 2222
    assert sorted(path.name for path in run.path.iterdir()) == ["serve.port"]


def test_remove_port_keeps_a_port_another_daemon_wrote(tmp_path: Path) -> None:
    run = RunDir(tmp_path / "run")
    run.write_port(2222)
    run.remove_port(owned_by=1111)
    assert run.read_port() == 2222


def test_remove_port_removes_the_port_it_wrote(tmp_path: Path) -> None:
    run = RunDir(tmp_path / "run")
    run.write_port(1111)
    run.remove_port(owned_by=1111)
    assert not run.port_path.exists()
