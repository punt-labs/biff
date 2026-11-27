"""``biff daemon install|uninstall|start|stop|restart|status`` and ``biff install``."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from biff.__main__ import app
from biff.run_dir import RunDir
from tests.test_supervision.fake_backend import FakeBackend
from tests.test_supervision.patching import host_has, no_claude, port_is

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

runner = CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway home that holds the ``biffd`` executable."""
    binary = tmp_path / ".local" / "bin" / "biffd"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    port_is(monkeypatch, "free")
    return tmp_path


@pytest.fixture
def backend(home: Path, monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    """The host has a service manager; this is its backend."""
    fake = FakeBackend(home / "units" / "biffd.service")
    host_has(monkeypatch, fake)
    return fake


@pytest.fixture
def no_manager(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The host has no service manager, as in a container."""
    host_has(monkeypatch, None)


class _Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps({"version": "9.9.9", "uptime_seconds": 12.5}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def live_biffd(home: Path) -> Iterator[int]:
    """A loopback listener that answers ``/health``, recorded as biffd's port."""
    server = HTTPServer(("127.0.0.1", 0), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    RunDir.default().write_port(port)
    yield port
    server.shutdown()
    server.server_close()


class TestDaemonInstall:
    def test_install_registers_the_unit(self, backend: FakeBackend) -> None:
        result = runner.invoke(app, ["daemon", "install"])
        assert (result.exit_code, backend.calls) == (0, ["install"]), result.output

    def test_install_reports_running_and_where(self, backend: FakeBackend) -> None:
        result = runner.invoke(app, ["daemon", "install"])
        assert "biffd running." in result.output
        assert str(backend.config_path()) in result.output

    def test_install_without_a_manager_fails_and_names_the_alternative(
        self, no_manager: None
    ) -> None:
        result = runner.invoke(app, ["daemon", "install"])
        assert (result.exit_code, "biff daemon serve" in result.output) == (1, True)

    def test_install_without_the_executable_names_it(
        self, backend: FakeBackend, tmp_path: Path
    ) -> None:
        (tmp_path / ".local" / "bin" / "biffd").unlink()
        result = runner.invoke(app, ["daemon", "install"])
        assert (result.exit_code, "Cannot find biffd" in result.output) == (1, True)

    def test_install_over_a_foreign_listener_is_refused(
        self, backend: FakeBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        port_is(monkeypatch, "foreign")
        result = runner.invoke(app, ["daemon", "install"])
        assert (result.exit_code, backend.calls) == (1, [])


class TestDaemonControl:
    def test_uninstall_removes_the_unit(self, backend: FakeBackend) -> None:
        runner.invoke(app, ["daemon", "install"])
        result = runner.invoke(app, ["daemon", "uninstall"])
        assert (result.exit_code, backend.config_path().exists()) == (0, False)

    def test_uninstall_without_a_manager_is_a_no_op(self, no_manager: None) -> None:
        result = runner.invoke(app, ["daemon", "uninstall"])
        assert result.exit_code == 0, result.output

    def test_start_before_install_names_the_repair(self, backend: FakeBackend) -> None:
        result = runner.invoke(app, ["daemon", "start"])
        assert (result.exit_code, "biff daemon install" in result.output) == (1, True)

    @pytest.mark.parametrize("verb", ["start", "stop", "restart"])
    def test_control_verbs_reach_the_supervisor(
        self, backend: FakeBackend, verb: str
    ) -> None:
        runner.invoke(app, ["daemon", "install"])
        result = runner.invoke(app, ["daemon", verb])
        assert (result.exit_code, backend.calls[-1]) == (0, verb), result.output

    def test_a_refused_restart_exits_1_and_names_the_log(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        refusing = FakeBackend(home / "units" / "biffd.service", ok=False)
        refusing.config_path().parent.mkdir(parents=True)
        refusing.config_path().write_text("unit")
        host_has(monkeypatch, refusing)
        result = runner.invoke(app, ["daemon", "restart"])
        assert (result.exit_code, "biff.log" in result.output) == (1, True)

    @pytest.mark.parametrize("verb", ["start", "stop", "restart"])
    def test_control_verbs_without_a_manager_fail(
        self, no_manager: None, verb: str
    ) -> None:
        result = runner.invoke(app, ["daemon", verb])
        assert result.exit_code == 1


class TestDaemonStatus:
    def test_not_installed(self, backend: FakeBackend) -> None:
        result = runner.invoke(app, ["daemon", "status"])
        assert (result.exit_code, "installed: no" in result.output) == (1, True)

    def test_installed_and_running_reports_version_and_port(
        self, backend: FakeBackend, live_biffd: int
    ) -> None:
        runner.invoke(app, ["daemon", "install"])
        result = runner.invoke(app, ["daemon", "status"])
        assert result.exit_code == 0, result.output
        assert "installed: yes" in result.output
        assert "running: yes" in result.output
        assert f"port: {live_biffd}" in result.output
        assert "version: 9.9.9" in result.output

    def test_installed_but_not_answering_exits_1(
        self, backend: FakeBackend, home: Path
    ) -> None:
        runner.invoke(app, ["daemon", "install"])
        RunDir.default().write_port(1)  # nothing listens on port 1
        result = runner.invoke(app, ["daemon", "status"])
        assert (result.exit_code, "health: not answering" in result.output) == (1, True)

    def test_without_a_manager_status_says_so(self, no_manager: None) -> None:
        result = runner.invoke(app, ["daemon", "status"])
        assert "service manager: none" in result.output


class TestBiffInstall:
    """``biff install`` installs biffd's service after the home tree."""

    @pytest.fixture(autouse=True)
    def _cli_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        no_claude(monkeypatch)

    def test_install_installs_the_service(self, backend: FakeBackend) -> None:
        result = runner.invoke(app, ["install"])
        assert (result.exit_code, backend.calls) == (0, ["install"]), result.output

    def test_install_says_the_service_is_running(self, backend: FakeBackend) -> None:
        result = runner.invoke(app, ["install"])
        assert "biffd running." in result.output

    def test_install_without_a_manager_notes_the_autostart(
        self, no_manager: None
    ) -> None:
        result = runner.invoke(app, ["install"])
        assert (result.exit_code, "starts on demand" in result.output) == (0, True)

    def test_install_reports_a_failed_service_install_and_exits_1(
        self, backend: FakeBackend, tmp_path: Path
    ) -> None:
        (tmp_path / ".local" / "bin" / "biffd").unlink()
        result = runner.invoke(app, ["install"])
        assert (result.exit_code, "Cannot find biffd" in result.output) == (1, True)

    def test_uninstall_removes_the_service(self, backend: FakeBackend) -> None:
        runner.invoke(app, ["install"])
        result = runner.invoke(app, ["uninstall"])
        assert (result.exit_code, backend.config_path().exists()) == (0, False)
