"""``biff daemon install|uninstall|start|stop|restart|status`` and ``biff install``."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from biff.__main__ import app
from biff.plugins.claude_code.claude_md import ClaudeMdImport
from biff.run_dir import RunDir
from tests.test_supervision.fake_backend import FakeBackend, RaisingBackend
from tests.test_supervision.patching import (
    host_has,
    host_unreachable,
    leftover_is,
    no_claude,
    port_answers,
    port_is,
)

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
    # biffd is only in the home above, not in the venv's bin on PATH.
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "py" / "python"))
    port_is(monkeypatch, "free")
    port_answers(monkeypatch, answers=True)
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

    def test_install_without_a_manager_exits_3_and_names_the_alternative(
        self, no_manager: None
    ) -> None:
        result = runner.invoke(app, ["daemon", "install"])
        assert (result.exit_code, "biff daemon serve" in result.output) == (3, True)

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
        assert result.exit_code == 3


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
        assert (result.exit_code, "biff daemon serve" in result.output) == (0, True)

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


class TestStatusNamesWhatItCannotShow:
    def test_relay_connections_and_clients_are_reported_as_not_yet_available(
        self, backend: FakeBackend, live_biffd: int
    ) -> None:
        runner.invoke(app, ["daemon", "install"])
        result = runner.invoke(app, ["daemon", "status"])
        assert "relay connections: not reported by biffd yet" in result.output
        assert "attached clients: not reported by biffd yet" in result.output


class TestHostFailuresAreOutcomesNotTracebacks:
    @pytest.mark.parametrize(
        "error",
        [
            subprocess.TimeoutExpired("systemctl", 30),
            PermissionError("cannot write unit"),
            FileNotFoundError("systemctl"),
        ],
        ids=["hung", "unwritable", "missing"],
    )
    def test_a_raising_supervisor_fails_the_command_cleanly(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException
    ) -> None:
        host_has(monkeypatch, RaisingBackend(home / "u.service", error))
        result = runner.invoke(app, ["daemon", "install"])
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)

    def test_biff_install_still_installs_the_plugin_step_when_the_service_raises(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        no_claude(monkeypatch)
        host_has(
            monkeypatch,
            RaisingBackend(
                home / "u.service", subprocess.TimeoutExpired("systemctl", 30)
            ),
        )
        result = runner.invoke(app, ["install"])
        assert (result.exit_code, "Installed (CLI-only)" in result.output) == (1, True)

    def test_biff_uninstall_strips_the_import_when_the_service_raises(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        no_claude(monkeypatch)
        line = "@~/.punt-labs/biff/CLAUDE.md"
        host_md = home / ".claude" / "CLAUDE.md"
        ClaudeMdImport(host_md, line).register()
        host_has(
            monkeypatch, RaisingBackend(home / "u.service", PermissionError("denied"))
        )
        result = runner.invoke(app, ["uninstall"])
        assert (result.exit_code, line in host_md.read_text()) == (1, False)
        assert isinstance(result.exception, SystemExit)


class TestAnUnreachableManagerIsNotAbsent:
    REASON = "systemctl --user show-environment failed: Failed to connect to bus"

    def test_install_says_why_on_stderr_and_still_exits_0(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        no_claude(monkeypatch)
        host_unreachable(monkeypatch, self.REASON)
        result = runner.invoke(app, ["install"])
        assert result.exit_code == 0
        assert "Failed to connect to bus" in result.stderr

    def test_the_daemon_command_exits_3_with_the_reason(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host_unreachable(monkeypatch, self.REASON)
        result = runner.invoke(app, ["daemon", "install"])
        assert (result.exit_code, "Failed to connect to bus" in result.output) == (
            3,
            True,
        )

    def test_an_absent_manager_carries_no_reason(self, no_manager: None) -> None:
        result = runner.invoke(app, ["daemon", "install"])
        assert "show-environment" not in result.output


class TestUninstallWhereTheManagerCannotBeReached:
    def test_a_leftover_unit_is_a_failure_that_names_the_command(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        unit = home / ".config" / "systemd" / "user" / "biffd.service"
        host_unreachable(monkeypatch, "Failed to connect to bus")
        leftover_is(monkeypatch, unit)
        result = runner.invoke(app, ["daemon", "uninstall"])
        assert result.exit_code == 1
        assert str(unit) in result.output
        assert "Failed to connect to bus" in result.output
        assert "biff daemon uninstall" in result.output

    def test_biff_uninstall_fails_too(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        no_claude(monkeypatch)
        host_unreachable(monkeypatch, "bus is down")
        leftover_is(monkeypatch, home / "biffd.service")
        assert runner.invoke(app, ["uninstall"]).exit_code == 1

    def test_nothing_installed_is_success(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host_unreachable(monkeypatch, "bus is down")
        assert runner.invoke(app, ["daemon", "uninstall"]).exit_code == 0
