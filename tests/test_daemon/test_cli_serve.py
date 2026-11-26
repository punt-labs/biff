"""``biff daemon serve`` fails with one actionable line, not a traceback."""

from __future__ import annotations

import os
import socket
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from biff.cli_app import CliApp
from biff.daemon.environment import DaemonEnvironment

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()
app = CliApp.build()


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
    assert result.exit_code == 1
    assert f"port {port} is already in use" in result.output
    assert "Traceback" not in result.output
    assert len(result.output.strip().splitlines()) == 1


def test_a_loose_run_directory_names_the_chmod(home: Path) -> None:
    run = home / ".punt-labs" / "biff" / "run"
    run.mkdir(parents=True, mode=0o755)
    run.chmod(0o755)
    result = runner.invoke(app, ["daemon", "serve", "--port", "0"])
    assert result.exit_code == 1
    assert "chmod 700" in result.output
    assert "Traceback" not in result.output


def test_a_corrupt_token_file_names_the_repair(home: Path) -> None:
    run = home / ".punt-labs" / "biff" / "run"
    run.mkdir(parents=True, mode=0o700)
    (run / "serve.token").write_text("")
    (run / "serve.token").chmod(0o600)
    result = runner.invoke(app, ["daemon", "serve", "--port", "0"])
    assert result.exit_code == 1
    assert "remove it" in result.output
    assert "Traceback" not in result.output


def test_serve_drops_its_own_identity_variables_and_names_them(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIFF_USER", "ambient")
    monkeypatch.setenv("BIFF_RELAY_URL", "nats://ambient")
    run = home / ".punt-labs" / "biff" / "run"
    run.mkdir(parents=True, mode=0o755)
    run.chmod(0o755)  # fail fast after the scrub, before binding
    result = runner.invoke(app, ["daemon", "serve", "--port", "0"])
    assert "BIFF_USER" in result.output
    assert "BIFF_RELAY_URL" in result.output
    assert "BIFF_USER" not in os.environ
    assert "BIFF_RELAY_URL" not in os.environ
