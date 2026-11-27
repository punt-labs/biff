"""The client's autostart nudge: connect, start biffd, wait, or say why not."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Final, Self, final

import pytest

from biff.client.nudge import (
    DaemonDidNotStartError,
    DaemonNotInstalledError,
    DaemonNudge,
    DaemonPortFileError,
    DaemonStartFailedError,
)
from biff.run_dir import RunDir
from biff.supervision.biffd import BIFFD_SPEC
from biff.supervision.errors import ServiceActionFailedError
from biff.supervision.health import HealthProbe
from biff.supervision.starter import HostStarter
from tests.test_supervision.fake_backend import FakeBackend, RaisingBackend

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from biff.supervision.starter import StartOutcome

NOT_INSTALLED: Final = "biffd is not installed. Run: biff install"
DID_NOT_START: Final = (
    "biffd is installed but did not start within 5 seconds. "
    "Run: biff daemon status, then biff doctor"
)


@final
class ScriptedProbe:
    """Answers ``/health`` only after it has been asked *after* times."""

    __slots__ = ("_after", "_asked", "_port")

    _after: int
    _asked: int
    _port: int

    def __new__(cls, after: int, *, port: int = 8424) -> Self:
        """Answer on *port* once asked more than *after* times."""
        self = super().__new__(cls)
        self._after = after
        self._asked = 0
        self._port = port
        return self

    @property
    def asked(self) -> int:
        return self._asked

    def live_port(self, timeout: float) -> int | None:
        self._asked += 1
        return self._port if self._asked > self._after else None


@final
class ScriptedStarter:
    """Reports a fixed outcome and counts how often it was asked to start."""

    __slots__ = ("_error", "_note", "_outcome", "_starts")

    _outcome: StartOutcome
    _error: bool
    _note: str
    _starts: int

    def __new__(
        cls, outcome: StartOutcome, *, error: bool = False, note: str = ""
    ) -> Self:
        self = super().__new__(cls)
        self._outcome = outcome
        self._error = error
        self._note = note
        self._starts = 0
        return self

    @property
    def starts(self) -> int:
        return self._starts

    def start(self) -> StartOutcome:
        self._starts += 1
        if self._error:
            raise ServiceActionFailedError("biffd start failed: unit is masked.")
        return self._outcome

    def exit_note(self) -> str:
        return self._note


def nudge(probe: ScriptedProbe, starter: ScriptedStarter) -> DaemonNudge:
    return DaemonNudge(probe, starter, patience=0.3, interval=0.01)


class TestEnsureRunning:
    def test_a_running_daemon_is_not_started_again(self) -> None:
        probe, starter = ScriptedProbe(after=0), ScriptedStarter("launched")
        nudge(probe, starter).ensure_running()
        assert starter.starts == 0

    def test_a_down_daemon_is_started_and_awaited(self) -> None:
        probe, starter = ScriptedProbe(after=3), ScriptedStarter("launched")
        nudge(probe, starter).ensure_running()
        assert (starter.starts, probe.asked) == (1, 4)

    def test_not_installed_says_to_install(self) -> None:
        with pytest.raises(DaemonNotInstalledError) as caught:
            nudge(
                ScriptedProbe(after=99), ScriptedStarter("not_installed")
            ).ensure_running()
        assert str(caught.value) == NOT_INSTALLED

    def test_installed_but_down_says_to_diagnose(self) -> None:
        with pytest.raises(DaemonDidNotStartError) as caught:
            nudge(
                ScriptedProbe(after=10_000), ScriptedStarter("launched")
            ).ensure_running()
        assert str(caught.value) == DID_NOT_START

    def test_a_refused_start_says_so_and_does_not_claim_a_timeout(self) -> None:
        with pytest.raises(DaemonStartFailedError) as caught:
            nudge(
                ScriptedProbe(after=99), ScriptedStarter("launched", error=True)
            ).ensure_running()
        assert "did not start within" not in str(caught.value)
        assert "unit is masked" in str(caught.value)
        assert "Run: biff daemon status, then biff doctor" in str(caught.value)

    def test_a_child_that_exited_is_reported_at_once_with_its_status(self) -> None:
        probe = ScriptedProbe(after=10_000)
        starter = ScriptedStarter("launched", note="biffd exited with status 78")
        started = time.monotonic()
        with pytest.raises(DaemonStartFailedError, match="status 78"):
            DaemonNudge(probe, starter, patience=30.0, interval=0.01).ensure_running()
        assert time.monotonic() - started < 2.0

    def test_a_corrupt_port_file_is_a_connection_error(self, tmp_path: Path) -> None:
        run_dir = RunDir(tmp_path / "run")
        run_dir.ensure()
        run_dir.port_path.write_text("not a port\n")
        with pytest.raises(DaemonPortFileError, match=r"serve\.port"):
            DaemonNudge.for_host(run_dir)

    def test_a_stale_recorded_port_falls_back_to_the_unit_port(
        self, tmp_path: Path, health_port: int
    ) -> None:
        run_dir = RunDir(tmp_path / "run")
        run_dir.write_port(1)  # a crash left a port nothing listens on
        spec = dataclasses.replace(BIFFD_SPEC, health_port=health_port)
        DaemonNudge.for_host(run_dir, spec).ensure_running()

    def test_it_returns_the_port_that_answered_not_the_stale_record(
        self, tmp_path: Path, health_port: int
    ) -> None:
        run_dir = RunDir(tmp_path / "run")
        run_dir.write_port(1)  # a custom port nothing listens on any more
        spec = dataclasses.replace(BIFFD_SPEC, health_port=health_port)
        assert DaemonNudge.for_host(run_dir, spec).ensure_running() == health_port

    def test_it_returns_the_recorded_port_when_that_one_answers(
        self, tmp_path: Path, health_port: int
    ) -> None:
        run_dir = RunDir(tmp_path / "run")
        run_dir.write_port(health_port)  # a --port override that is healthy
        spec = dataclasses.replace(BIFFD_SPEC, health_port=1)
        assert DaemonNudge.for_host(run_dir, spec).ensure_running() == health_port

    def test_a_daemon_that_comes_up_after_a_start_returns_its_port(self) -> None:
        probe = ScriptedProbe(after=3, port=9001)
        assert nudge(probe, ScriptedStarter("launched")).ensure_running() == 9001

    def test_the_two_failures_are_distinct_types(self) -> None:
        assert not issubclass(DaemonNotInstalledError, DaemonDidNotStartError)

    def test_both_failures_are_connection_errors(self) -> None:
        assert issubclass(DaemonNotInstalledError, ConnectionError) and issubclass(
            DaemonDidNotStartError, ConnectionError
        )


@final
class FakeChild:
    """A launched process that has exited with *status*, or is still running."""

    __slots__ = ("_status",)

    _status: int | None

    def __new__(cls, status: int | None) -> Self:
        self = super().__new__(cls)
        self._status = status
        return self

    def poll(self) -> int | None:
        return self._status


@final
class FakeDetacher:
    """Records a detached launch and answers where the binary is."""

    __slots__ = ("_error", "_found", "_status", "launched")

    _found: Path | None
    _status: int | None
    _error: OSError | None
    launched: list[tuple[Path, int]]

    def __new__(
        cls,
        found: Path | None,
        *,
        status: int | None = None,
        error: OSError | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._found = found
        self._status = status
        self._error = error
        self.launched = []
        return self

    def binary(self) -> Path | None:
        return self._found

    def launch(self, binary: Path, port: int) -> FakeChild:
        if self._error is not None:
            raise self._error
        self.launched.append((binary, port))
        return FakeChild(self._status)


class TestHostStarter:
    """What "start" and "installed" mean on each kind of host."""

    def test_unit_missing_means_not_installed(self, tmp_path: Path) -> None:
        backend = FakeBackend(tmp_path / "biffd.service")
        starter = HostStarter(
            BIFFD_SPEC, 8424, backend, FakeDetacher(tmp_path / "biffd")
        )
        assert (starter.start(), backend.calls) == ("not_installed", [])

    def test_unit_present_is_started_through_the_manager(self, tmp_path: Path) -> None:
        unit = tmp_path / "biffd.service"
        unit.write_text("unit")
        backend = FakeBackend(unit)
        detacher = FakeDetacher(tmp_path / "biffd")
        outcome = HostStarter(BIFFD_SPEC, 8424, backend, detacher).start()
        assert (outcome, backend.calls, detacher.launched) == (
            "launched",
            ["start"],
            [],
        )

    def test_a_refusing_manager_raises(self, tmp_path: Path) -> None:
        unit = tmp_path / "biffd.service"
        unit.write_text("unit")
        starter = HostStarter(
            BIFFD_SPEC, 8424, FakeBackend(unit, ok=False), FakeDetacher(None)
        )
        with pytest.raises(ServiceActionFailedError):
            starter.start()

    def test_no_manager_and_no_binary_means_not_installed(self) -> None:
        assert HostStarter(BIFFD_SPEC, 8424, None, FakeDetacher(None)).start() == (
            "not_installed"
        )

    def test_a_popen_failure_is_a_failed_start_not_an_oserror(
        self, tmp_path: Path
    ) -> None:
        detacher = FakeDetacher(tmp_path / "biffd", error=PermissionError("denied"))
        starter = HostStarter(BIFFD_SPEC, 8424, None, detacher)
        with pytest.raises(ServiceActionFailedError, match="denied"):
            starter.start()

    def test_a_missing_systemctl_is_a_failed_start(self, tmp_path: Path) -> None:
        unit = tmp_path / "biffd.service"
        unit.write_text("unit")
        starter = HostStarter(
            BIFFD_SPEC,
            8424,
            RaisingBackend(unit, FileNotFoundError("systemctl")),
            FakeDetacher(None),
        )
        with pytest.raises(ServiceActionFailedError, match="systemctl"):
            starter.start()

    def test_a_hung_supervisor_is_a_failed_start(self, tmp_path: Path) -> None:
        unit = tmp_path / "biffd.service"
        unit.write_text("unit")
        hung = subprocess.TimeoutExpired("systemctl", 30)
        starter = HostStarter(
            BIFFD_SPEC, 8424, RaisingBackend(unit, hung), FakeDetacher(None)
        )
        with pytest.raises(ServiceActionFailedError, match="timed out"):
            starter.start()

    def test_the_exit_note_names_a_child_that_died(self, tmp_path: Path) -> None:
        detacher = FakeDetacher(tmp_path / "biffd", status=78)
        starter = HostStarter(BIFFD_SPEC, 8424, None, detacher)
        starter.start()
        assert "status 78" in starter.exit_note()

    def test_the_exit_note_is_empty_while_the_child_runs(self, tmp_path: Path) -> None:
        starter = HostStarter(BIFFD_SPEC, 8424, None, FakeDetacher(tmp_path / "b"))
        starter.start()
        assert starter.exit_note() == ""

    def test_no_manager_launches_the_binary_detached_on_the_port(
        self, tmp_path: Path
    ) -> None:
        binary = tmp_path / "biffd"
        detacher = FakeDetacher(binary)
        outcome = HostStarter(BIFFD_SPEC, 9001, None, detacher).start()
        assert (outcome, detacher.launched) == ("launched", [(binary, 9001)])


class _Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps({"version": "9.9.9", "uptime_seconds": 1.5}).encode()
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def health_port() -> Iterator[int]:
    """A real loopback listener that answers ``/health`` as biffd does."""
    server = HTTPServer(("127.0.0.1", 0), _Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


class TestForHost:
    def test_a_live_listener_needs_no_start(
        self, tmp_path: Path, health_port: int
    ) -> None:
        run_dir = RunDir(tmp_path / "run")
        run_dir.write_port(health_port)
        assert DaemonNudge.for_host(run_dir).ensure_running() == health_port

    def test_health_probe_reads_version_and_uptime(self, health_port: int) -> None:
        health = HealthProbe(health_port).fetch(1.0)
        assert (health.version, health.uptime_seconds) == ("9.9.9", 1.5)
