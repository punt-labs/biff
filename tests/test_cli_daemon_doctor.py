"""``biff doctor``'s seven biffd checks, run against real listeners and files."""

from __future__ import annotations

import dataclasses
import json
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.metadata import PackageNotFoundError
from typing import TYPE_CHECKING, Final

import pytest

from biff.cli_daemon_doctor import DaemonChecks, DaemonDiagnostics
from biff.plugins.contract import CheckResult, PluginRegistry
from biff.run_dir import RunDir
from biff.supervision.biffd import BIFFD_SPEC
from biff.supervision.host import Detection
from tests.test_supervision.fake_backend import FakeBackend, RaisingBackend

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

CLIENT_VERSION: Final = "9.9.9"
NAMES: Final = (
    "biffd unit",
    "biffd running",
    "biffd port",
    "biffd health",
    "biffd token",
    "biffd version",
    "biffd relay",
)


class _Health(BaseHTTPRequestHandler):
    version = CLIENT_VERSION

    def do_GET(self) -> None:
        body = json.dumps({"version": self.version, "uptime_seconds": 3}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class _OldHealth(_Health):
    version = "1.0.0"


class _NotBiffd(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<html>not biffd</html>")

    def log_message(self, format: str, *args: object) -> None:
        return


def _serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[int]:
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.fixture
def biffd_port() -> Iterator[int]:
    yield from _serve(_Health)


@pytest.fixture
def old_biffd_port() -> Iterator[int]:
    yield from _serve(_OldHealth)


@pytest.fixture
def foreign_port() -> Iterator[int]:
    yield from _serve(_NotBiffd)


@pytest.fixture
def free_port() -> int:
    """A loopback port nothing listens on: bind, note the number, release."""
    server = HTTPServer(("127.0.0.1", 0), _Health)
    port = int(server.server_address[1])
    server.server_close()
    return port


@pytest.fixture
def run_dir(tmp_path: Path) -> RunDir:
    directory = RunDir(tmp_path / "run")
    directory.ensure_token()
    return directory


@pytest.fixture
def unit(tmp_path: Path) -> FakeBackend:
    backend = FakeBackend(tmp_path / "biffd.service")
    backend.install()
    return backend


def checks(
    backend: FakeBackend | RaisingBackend | None,
    run_dir: RunDir,
    port: int,
    *,
    host: Detection | None = None,
) -> dict[str, CheckResult]:
    run_dir.write_port(port)
    found = host or (
        Detection("absent", None, "")
        if backend is None
        else Detection("found", backend, "")
    )
    spec = dataclasses.replace(BIFFD_SPEC, health_port=port)
    return {
        result.name: result
        for result in DaemonChecks(found, run_dir, CLIENT_VERSION, spec=spec).checks()
    }


def test_all_seven_checks_pass_for_a_healthy_daemon(
    unit: FakeBackend, run_dir: RunDir, biffd_port: int
) -> None:
    results = checks(unit, run_dir, biffd_port)
    assert (tuple(results), [r.passed for r in results.values()]) == (
        NAMES,
        [True] * 7,
    )


class TestEachFailureNamesItsRepair:
    def test_unit_missing(
        self, tmp_path: Path, run_dir: RunDir, biffd_port: int
    ) -> None:
        results = checks(FakeBackend(tmp_path / "none.service"), run_dir, biffd_port)
        unit = results["biffd unit"]
        assert (unit.passed, "biff daemon install" in unit.message) == (False, True)

    def test_unit_not_running(
        self, unit: FakeBackend, run_dir: RunDir, biffd_port: int
    ) -> None:
        unit.stop()
        running = checks(unit, run_dir, biffd_port)["biffd running"]
        assert (running.passed, "biff daemon start" in running.message) == (False, True)

    def test_port_free(
        self, unit: FakeBackend, run_dir: RunDir, free_port: int
    ) -> None:
        port = checks(unit, run_dir, free_port)["biffd port"]
        assert (port.passed, "biff daemon start" in port.message) == (False, True)

    def test_port_held_by_something_else(
        self, unit: FakeBackend, run_dir: RunDir, foreign_port: int
    ) -> None:
        port = checks(unit, run_dir, foreign_port)["biffd port"]
        assert (port.passed, f"lsof -nP -iTCP:{foreign_port}" in port.message) == (
            False,
            True,
        )

    def test_health_silent(
        self, unit: FakeBackend, run_dir: RunDir, free_port: int
    ) -> None:
        health = checks(unit, run_dir, free_port)["biffd health"]
        assert (health.passed, "biff daemon restart" in health.message) == (False, True)

    def test_token_missing(
        self, unit: FakeBackend, run_dir: RunDir, biffd_port: int
    ) -> None:
        run_dir.token_path.unlink()
        token = checks(unit, run_dir, biffd_port)["biffd token"]
        assert (token.passed, "biff daemon restart" in token.message) == (False, True)

    def test_token_readable_by_others(
        self, unit: FakeBackend, run_dir: RunDir, biffd_port: int
    ) -> None:
        run_dir.token_path.chmod(0o644)
        token = checks(unit, run_dir, biffd_port)["biffd token"]
        assert (token.passed, "chmod 600" in token.message) == (False, True)

    def test_version_differs(
        self, unit: FakeBackend, run_dir: RunDir, old_biffd_port: int
    ) -> None:
        version = checks(unit, run_dir, old_biffd_port)["biffd version"]
        assert (
            version.passed,
            "daemon 1.0.0" in version.message,
            "client 9.9.9" in version.message,
            "biff daemon restart" in version.message,
        ) == (False, True, True, True)

    def test_version_unreadable_when_the_daemon_is_down(
        self, unit: FakeBackend, run_dir: RunDir, free_port: int
    ) -> None:
        version = checks(unit, run_dir, free_port)["biffd version"]
        assert (version.passed, "biff daemon start" in version.message) == (False, True)

    def test_relay_check_when_the_daemon_is_down_names_the_start(
        self, unit: FakeBackend, run_dir: RunDir, free_port: int
    ) -> None:
        relay = checks(unit, run_dir, free_port)["biffd relay"]
        assert (relay.passed, "biff daemon start" in relay.message) == (False, True)


class TestWithoutAServiceManager:
    def test_unit_check_says_biffd_starts_on_demand(
        self, run_dir: RunDir, biffd_port: int
    ) -> None:
        unit = checks(None, run_dir, biffd_port)["biffd unit"]
        assert (unit.passed, "biff daemon serve" in unit.message) == (True, True)

    def test_running_means_health_answers(
        self, run_dir: RunDir, biffd_port: int
    ) -> None:
        assert checks(None, run_dir, biffd_port)["biffd running"].passed


def test_diagnostics_prints_the_daemon_checks(
    unit: FakeBackend,
    run_dir: RunDir,
    biffd_port: int,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in (
        "_check_gh_cli",
        "_check_plugin_installed",
        "_check_user_commands",
        "_check_relay",
        "_check_biff_file",
        "_check_enabled",
        "_check_git_hooks",
        "_check_ci_workflow",
    ):
        monkeypatch.setattr(
            f"biff.doctor.{name}", lambda: CheckResult("other", True, "ok")
        )
    run_dir.write_port(biffd_port)
    source = DaemonChecks(Detection("found", unit, ""), run_dir, CLIENT_VERSION)

    def for_host() -> DaemonChecks:
        return source

    monkeypatch.setattr(DaemonChecks, "for_host", staticmethod(for_host))
    DaemonDiagnostics(PluginRegistry()).run()
    out = capsys.readouterr().out
    assert all(f"{name}:" in out for name in NAMES)


@pytest.fixture
def silent_port() -> Iterator[int]:
    """A listener that accepts connections and never answers."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    yield sock.getsockname()[1]
    sock.close()


class TestRequiredOnceInstalled:
    def test_version_and_foreign_port_are_required_when_the_unit_exists(
        self, unit: FakeBackend, run_dir: RunDir, biffd_port: int
    ) -> None:
        results = checks(unit, run_dir, biffd_port)
        assert (
            results["biffd version"].required,
            results["biffd port"].required,
            results["biffd health"].required,
            results["biffd running"].required,
        ) == (True, True, False, False)

    def test_a_version_mismatch_with_an_installed_unit_fails_the_doctor(
        self, unit: FakeBackend, run_dir: RunDir, old_biffd_port: int
    ) -> None:
        version = checks(unit, run_dir, old_biffd_port)["biffd version"]
        assert (version.passed, version.required) == (False, True)

    def test_a_foreign_port_holder_with_an_installed_unit_fails_the_doctor(
        self, unit: FakeBackend, run_dir: RunDir, foreign_port: int
    ) -> None:
        port = checks(unit, run_dir, foreign_port)["biffd port"]
        assert (port.passed, port.required) == (False, True)

    def test_nothing_is_required_when_the_unit_is_not_installed(
        self, tmp_path: Path, run_dir: RunDir, old_biffd_port: int
    ) -> None:
        results = checks(
            FakeBackend(tmp_path / "none.service"), run_dir, old_biffd_port
        )
        assert not any(r.required for r in results.values())

    def test_a_line_says_the_checks_are_advisory_without_a_unit(
        self, tmp_path: Path, run_dir: RunDir, biffd_port: int
    ) -> None:
        results = checks(FakeBackend(tmp_path / "none.service"), run_dir, biffd_port)
        assert "advisory" in results["biffd checks"].message

    def test_no_advisory_line_when_the_unit_is_installed(
        self, unit: FakeBackend, run_dir: RunDir, biffd_port: int
    ) -> None:
        assert "biffd checks" not in checks(unit, run_dir, biffd_port)


class TestDoctorNeverRaises:
    def test_a_hung_supervisor_is_a_failed_line(
        self, tmp_path: Path, run_dir: RunDir, biffd_port: int
    ) -> None:
        hung = RaisingBackend(
            tmp_path / "u", subprocess.TimeoutExpired("systemctl", 30)
        )
        running = checks(hung, run_dir, biffd_port)["biffd running"]
        assert (running.passed, "could not check" in running.message) == (False, True)

    def test_an_unreadable_unit_directory_is_a_failed_line(
        self, tmp_path: Path, run_dir: RunDir, biffd_port: int
    ) -> None:
        class _Unreadable(RaisingBackend):
            __slots__ = ()

            def config_path(self) -> Path:
                raise PermissionError("denied")

        unit = checks(_Unreadable(tmp_path / "u", OSError()), run_dir, biffd_port)[
            "biffd unit"
        ]
        assert (unit.passed, "could not check" in unit.message) == (False, True)

    def test_a_token_that_cannot_be_read_is_a_failed_line(
        self, unit: FakeBackend, run_dir: RunDir, biffd_port: int
    ) -> None:
        run_dir.token_path.unlink()
        run_dir.token_path.mkdir(mode=0o600)
        token = checks(unit, run_dir, biffd_port)["biffd token"]
        assert not token.passed

    def test_a_missing_package_is_a_failed_version_line_not_a_traceback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def missing(name: str) -> str:
            raise PackageNotFoundError(name)

        monkeypatch.setattr("biff.cli_daemon_doctor.package_version", missing)
        results = {r.name: r for r in DaemonChecks.for_host().checks()}
        assert "biffd version" in results

    def test_an_unreadable_port_file_is_a_failed_port_line(
        self, unit: FakeBackend, run_dir: RunDir, biffd_port: int
    ) -> None:
        run_dir.write_port(biffd_port)
        run_dir.port_path.unlink()
        run_dir.port_path.mkdir()
        spec = dataclasses.replace(BIFFD_SPEC, health_port=biffd_port)
        found = DaemonChecks(
            Detection("found", unit, ""),
            run_dir,
            CLIENT_VERSION,
            spec=spec,
        ).checks()
        assert {r.name: r for r in found}["biffd port"].passed is False


class TestHostReasons:
    def test_an_unreachable_manager_names_the_reason_on_the_unit_line(
        self, run_dir: RunDir, biffd_port: int
    ) -> None:
        host = Detection("unreachable", None, "Failed to connect to bus")
        unit = checks(None, run_dir, biffd_port, host=host)["biffd unit"]
        assert "Failed to connect to bus" in unit.message

    def test_an_unresponsive_port_names_the_restart(
        self, unit: FakeBackend, run_dir: RunDir, silent_port: int
    ) -> None:
        port = checks(unit, run_dir, silent_port)["biffd port"]
        assert (port.passed, "biff daemon restart" in port.message) == (False, True)


class TestRecordedPortFallback:
    def test_a_stale_recorded_port_falls_back_to_the_unit_port(
        self, unit: FakeBackend, run_dir: RunDir, biffd_port: int
    ) -> None:
        run_dir.write_port(1)
        spec = dataclasses.replace(BIFFD_SPEC, health_port=biffd_port)
        found = DaemonChecks(
            Detection("found", unit, ""),
            run_dir,
            CLIENT_VERSION,
            spec=spec,
        ).checks()
        assert {r.name: r for r in found}["biffd health"].passed


def test_the_relay_line_does_not_open_a_session_and_says_it_is_not_reported(
    unit: FakeBackend, run_dir: RunDir, biffd_port: int
) -> None:
    relay = checks(unit, run_dir, biffd_port)["biffd relay"]
    assert (relay.passed, "not reported by biffd yet" in relay.message) == (True, True)


class TestDoctorExitStatus:
    """``biff doctor`` itself exits non-zero for a daemon the machine asked for."""

    @pytest.fixture
    def doctor(
        self,
        unit: FakeBackend,
        run_dir: RunDir,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        for name in (
            "_check_gh_cli",
            "_check_plugin_installed",
            "_check_user_commands",
            "_check_relay",
            "_check_biff_file",
            "_check_enabled",
            "_check_git_hooks",
            "_check_ci_workflow",
        ):
            monkeypatch.setattr(
                f"biff.doctor.{name}", lambda: CheckResult("other", True, "ok")
            )

    def _run(
        self,
        unit: FakeBackend,
        run_dir: RunDir,
        port: int,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> tuple[int, str]:
        run_dir.write_port(port)
        spec = dataclasses.replace(BIFFD_SPEC, health_port=port)
        source = DaemonChecks(
            Detection("found", unit, ""), run_dir, CLIENT_VERSION, spec=spec
        )

        def for_host() -> DaemonChecks:
            return source

        monkeypatch.setattr(DaemonChecks, "for_host", staticmethod(for_host))
        code = DaemonDiagnostics(PluginRegistry()).run()
        return code, capsys.readouterr().out

    def test_a_version_mismatch_exits_1_and_does_not_say_all_passed(
        self,
        doctor: None,
        unit: FakeBackend,
        run_dir: RunDir,
        old_biffd_port: int,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        code, out = self._run(unit, run_dir, old_biffd_port, monkeypatch, capsys)
        assert code == 1
        assert "All required checks passed" not in out

    def test_a_matching_daemon_exits_0(
        self,
        doctor: None,
        unit: FakeBackend,
        run_dir: RunDir,
        biffd_port: int,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        code, _ = self._run(unit, run_dir, biffd_port, monkeypatch, capsys)
        assert code == 0
