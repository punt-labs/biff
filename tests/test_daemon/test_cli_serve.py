"""``biff daemon serve`` fails with one actionable line, not a traceback."""

from __future__ import annotations

import os
import socket
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from biff.cli_app import CliApp
from biff.daemon.environment import DaemonEnvironment
from biff.log_hint import LOG_HINT, LOG_PATH
from biff.supervision.biffd import BIFFD_SPEC

runner = CliRunner()
app = CliApp.build()
PERMANENT_EXIT = BIFFD_SPEC.permanent_exit_status


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in DaemonEnvironment.scrub(dict(os.environ)):
        monkeypatch.delenv(name)
    return tmp_path


def test_a_port_in_use_is_one_actionable_line(home: Path) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        result = runner.invoke(app, ["daemon", "serve", "--port", str(port)])
    assert result.exit_code == PERMANENT_EXIT
    assert f"port {port} is already in use" in result.output
    assert "Traceback" not in result.output
    # One biffd line for the user; the log's own line is the second.
    lines = result.output.strip().splitlines()
    assert len([line for line in lines if line.startswith("biffd:")]) == 1


def test_a_loose_run_directory_names_the_chmod(home: Path) -> None:
    run = home / ".punt-labs" / "biff" / "run"
    run.mkdir(parents=True, mode=0o755)
    run.chmod(0o755)
    result = runner.invoke(app, ["daemon", "serve", "--port", "0"])
    assert result.exit_code == PERMANENT_EXIT
    assert "chmod 700" in result.output
    assert "Traceback" not in result.output


def test_a_corrupt_token_file_names_the_repair(home: Path) -> None:
    run = home / ".punt-labs" / "biff" / "run"
    run.mkdir(parents=True, mode=0o700)
    (run / "serve.token").write_text("")
    (run / "serve.token").chmod(0o600)
    result = runner.invoke(app, ["daemon", "serve", "--port", "0"])
    assert result.exit_code == PERMANENT_EXIT
    assert "remove it" in result.output
    assert "Traceback" not in result.output


def test_serve_drops_its_own_identity_variables_and_names_them(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIFF_USER", "ambient")
    monkeypatch.setenv("BIFF_OWNER", "ambient")
    run = home / ".punt-labs" / "biff" / "run"
    run.mkdir(parents=True, mode=0o755)
    run.chmod(0o755)  # fail fast after the scrub, before binding
    result = runner.invoke(app, ["daemon", "serve", "--port", "0"])
    assert "BIFF_USER" in result.output
    assert "BIFF_OWNER" in result.output
    assert "BIFF_USER" not in os.environ
    assert "BIFF_OWNER" not in os.environ


def test_serve_keeps_the_relay_settings_it_was_started_with(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIFF_RELAY_URL", "nats://started-with")
    run = home / ".punt-labs" / "biff" / "run"
    run.mkdir(parents=True, mode=0o755)
    run.chmod(0o755)  # fail fast after the scrub, before binding
    result = runner.invoke(app, ["daemon", "serve", "--port", "0"])
    assert "BIFF_RELAY_URL" not in result.output
    assert os.environ["BIFF_RELAY_URL"] == "nats://started-with"


def test_serve_writes_the_log_file_that_internal_errors_point_at(
    home: Path,
) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        runner.invoke(app, ["daemon", "serve", "--port", str(taken.getsockname()[1])])
    assert Path(LOG_PATH).expanduser().is_file()


def test_a_startup_failure_leaves_its_reason_in_the_log(home: Path) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        runner.invoke(app, ["daemon", "serve", "--port", str(port)])
    assert f"port {port} is already in use" in Path(LOG_PATH).expanduser().read_text()


def test_a_permanent_failure_exits_with_the_status_the_unit_does_not_restart(
    home: Path,
) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        result = runner.invoke(
            app, ["daemon", "serve", "--port", str(taken.getsockname()[1])]
        )
    assert result.exit_code == 78


def test_the_hint_in_an_internal_error_names_that_same_file() -> None:
    assert LOG_PATH in LOG_HINT


def test_the_log_file_is_private_to_the_user(home: Path) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        runner.invoke(app, ["daemon", "serve", "--port", str(taken.getsockname()[1])])
    assert stat.S_IMODE(Path(LOG_PATH).expanduser().stat().st_mode) == 0o600
