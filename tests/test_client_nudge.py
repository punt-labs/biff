"""The client's autostart nudge: connect, start biffd, wait, or say why not."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Final, Self, final

import pytest

from biff.client.nudge import (
    DaemonDidNotStartError,
    DaemonNotInstalledError,
    DaemonNudge,
)
from biff.run_dir import RunDir
from biff.supervision.biffd import BIFFD_SPEC
from biff.supervision.errors import ServiceActionFailedError
from biff.supervision.health import HealthProbe
from biff.supervision.starter import HostStarter
from tests.test_supervision.fake_backend import FakeBackend

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

    __slots__ = ("_after", "_asked")

    _after: int
    _asked: int

    def __new__(cls, after: int) -> Self:
        self = super().__new__(cls)
        self._after = after
        self._asked = 0
        return self

    @property
    def asked(self) -> int:
        return self._asked

    def answers(self, timeout: float) -> bool:
        self._asked += 1
        return self._asked > self._after


@final
class ScriptedStarter:
    """Reports a fixed outcome and counts how often it was asked to start."""

    __slots__ = ("_error", "_outcome", "_starts")

    _outcome: StartOutcome
    _error: bool
    _starts: int

    def __new__(cls, outcome: StartOutcome, *, error: bool = False) -> Self:
        self = super().__new__(cls)
        self._outcome = outcome
        self._error = error
        self._starts = 0
        return self

    @property
    def starts(self) -> int:
        return self._starts

    def start(self) -> StartOutcome:
        self._starts += 1
        if self._error:
            raise ServiceActionFailedError("biffd start failed. See the log.")
        return self._outcome


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

    def test_a_refused_start_is_installed_but_down(self) -> None:
        with pytest.raises(DaemonDidNotStartError) as caught:
            nudge(
                ScriptedProbe(after=99), ScriptedStarter("launched", error=True)
            ).ensure_running()
        assert str(caught.value) == DID_NOT_START

    def test_the_two_failures_are_distinct_types(self) -> None:
        assert not issubclass(DaemonNotInstalledError, DaemonDidNotStartError)

    def test_both_failures_are_connection_errors(self) -> None:
        assert issubclass(DaemonNotInstalledError, ConnectionError) and issubclass(
            DaemonDidNotStartError, ConnectionError
        )


@final
class FakeDetacher:
    """Records a detached launch and answers where the binary is."""

    __slots__ = ("_found", "launched")

    _found: Path | None
    launched: list[tuple[Path, int]]

    def __new__(cls, found: Path | None) -> Self:
        self = super().__new__(cls)
        self._found = found
        self.launched = []
        return self

    def binary(self) -> Path | None:
        return self._found

    def launch(self, binary: Path, port: int) -> None:
        self.launched.append((binary, port))


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
        DaemonNudge.for_host(run_dir).ensure_running()

    def test_health_probe_reads_version_and_uptime(self, health_port: int) -> None:
        health = HealthProbe(health_port).fetch(1.0)
        assert (health.version, health.uptime_seconds) == ("9.9.9", 1.5)
