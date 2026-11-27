"""``biff doctor``'s seven biffd checks, run against real listeners and files."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Final, Self, final

import pytest

from biff.cli_daemon_doctor import DaemonChecks, DaemonDiagnostics
from biff.plugins.contract import CheckResult, PluginRegistry
from biff.run_dir import RunDir
from tests.test_supervision.fake_backend import FakeBackend

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


@final
class StubRelay:
    """Reports a fixed relay line, or raises the failure it was given."""

    __slots__ = ("_failure",)

    _failure: str

    def __new__(cls, failure: str = "") -> Self:
        self = super().__new__(cls)
        self._failure = failure
        return self

    def status(self, port: int) -> str:
        if self._failure:
            raise ConnectionError(self._failure)
        return "relay: local"


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
    backend: FakeBackend | None,
    run_dir: RunDir,
    port: int,
    relay: StubRelay | None = None,
) -> dict[str, CheckResult]:
    run_dir.write_port(port)
    found = DaemonChecks(
        backend, run_dir, relay or StubRelay(), CLIENT_VERSION
    ).checks()
    return {result.name: result for result in found}


def test_all_seven_checks_pass_for_a_healthy_daemon(
    unit: FakeBackend, run_dir: RunDir, biffd_port: int
) -> None:
    results = checks(unit, run_dir, biffd_port)
    assert (tuple(results), [r.passed for r in results.values()]) == (
        NAMES,
        [True] * 7,
    )


def test_checks_are_informational_until_the_daemon_is_the_default_path(
    unit: FakeBackend, run_dir: RunDir, biffd_port: int
) -> None:
    results = checks(unit, run_dir, biffd_port)
    assert not any(r.required for r in results.values())


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

    def test_relay_down_says_why(
        self, unit: FakeBackend, run_dir: RunDir, biffd_port: int
    ) -> None:
        relay = checks(unit, run_dir, biffd_port, StubRelay("NATS refused"))[
            "biffd relay"
        ]
        assert (relay.passed, "NATS refused" in relay.message) == (False, True)

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
        assert (unit.passed, "starts on demand" in unit.message) == (True, True)

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
    source = DaemonChecks(unit, run_dir, StubRelay(), CLIENT_VERSION)

    def for_host() -> DaemonChecks:
        return source

    monkeypatch.setattr(DaemonChecks, "for_host", staticmethod(for_host))
    DaemonDiagnostics(PluginRegistry()).run()
    out = capsys.readouterr().out
    assert all(f"{name}:" in out for name in NAMES)
