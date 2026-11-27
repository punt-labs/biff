"""How long a start waits for biffd, and what a timeout says about the service."""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

import pytest

from biff.supervision.biffd import BIFFD_SPEC
from biff.supervision.errors import ServiceActionFailedError
from biff.supervision.health_wait import HealthWait
from biff.supervision.launchd import LaunchdBackend
from biff.supervision.manager import ServiceManager
from biff.supervision.port import PortProbe
from biff.supervision.systemd import SystemdBackend
from tests.test_supervision.fake_backend import (
    DiagnosableBackend,
    FakeBackend,
    port_probe,
)

if TYPE_CHECKING:
    from pathlib import Path

    from tests.test_supervision.scripted import ScriptedSupervisor

VARIABLE = HealthWait.ENVIRONMENT_VARIABLE

# The shape of ``launchctl print gui/<uid>/<label>``: the exit code comes well
# after the first eight lines.
LAUNCHCTL_PRINT = "\n".join(
    [
        "gui/501/com.punt-labs.biffd = {",
        "\tactive count = 0",
        "\tpath = /Users/me/Library/LaunchAgents/com.punt-labs.biffd.plist",
        "\ttype = LaunchAgent",
        "\tstate = not running",
        "",
        "\tprogram = /Users/me/.local/bin/biffd",
        "\targuments = {",
        "\t\t/Users/me/.local/bin/biffd",
        "\t\t--port",
        "\t\t8424",
        "\t}",
        "",
        "\tstdout path = /Users/me/.punt-labs/biff/logs/biff.log",
        "\tdomain = gui/501 [100001]",
        "\truns = 3",
        "\tlast exit code = 78",
        "}",
    ]
)


def _install_that_times_out(backend: FakeBackend) -> str:
    manager = ServiceManager(BIFFD_SPEC, backend, port_probe("free", answers=False))
    with pytest.raises(ServiceActionFailedError) as caught:
        manager.install()
    return str(caught.value)


class TestHealthWait:
    def test_the_default_is_thirty_seconds(self) -> None:
        assert HealthWait.from_environ({}) == HealthWait(30.0)

    def test_the_variable_sets_the_wait(self) -> None:
        assert HealthWait.from_environ({VARIABLE: "90"}) == HealthWait(90.0)

    def test_a_fraction_of_a_second_is_allowed(self) -> None:
        assert HealthWait.from_environ({VARIABLE: "0.5"}) == HealthWait(0.5)

    def test_a_blank_variable_is_the_default(self) -> None:
        assert HealthWait.from_environ({VARIABLE: "  "}) == HealthWait(30.0)

    @pytest.mark.parametrize("raw", ["soon", "0", "-5", "nan", "inf", "601"])
    def test_a_value_that_is_not_a_wait_is_refused_by_name(self, raw: str) -> None:
        with pytest.raises(ServiceActionFailedError, match=VARIABLE) as caught:
            HealthWait.from_environ({VARIABLE: raw})
        assert repr(raw) in str(caught.value)


class TestTheWaitReachesTheService:
    def test_install_waits_thirty_seconds_by_default(self, tmp_path: Path) -> None:
        port = port_probe("free")
        ServiceManager(BIFFD_SPEC, FakeBackend(tmp_path / "u"), port).install()
        assert port.waits == [30.0]

    def test_install_follows_the_variable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(VARIABLE, "120")
        port = port_probe("free")
        ServiceManager(BIFFD_SPEC, FakeBackend(tmp_path / "u"), port).install()
        assert port.waits == [120.0]

    def test_the_timeout_names_the_wait_that_was_given(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(VARIABLE, "45")
        assert "within 45 seconds" in _install_that_times_out(
            FakeBackend(tmp_path / "u")
        )

    def test_a_bad_variable_stops_install_before_it_registers_anything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(VARIABLE, "soon")
        backend = FakeBackend(tmp_path / "u")
        manager = ServiceManager(BIFFD_SPEC, backend, port_probe("free"))
        with pytest.raises(ServiceActionFailedError, match=VARIABLE):
            manager.install()
        assert backend.calls == []

    @pytest.mark.parametrize("action", ["start", "restart"])
    def test_start_and_restart_wait_for_health(
        self, tmp_path: Path, action: str
    ) -> None:
        backend = FakeBackend(tmp_path / "u")
        backend.install()
        port = port_probe("free")
        getattr(ServiceManager(BIFFD_SPEC, backend, port), action)()
        assert port.waits == [30.0]

    @pytest.mark.parametrize("action", ["start", "restart"])
    def test_a_start_that_never_answers_fails_and_stays_installed(
        self, tmp_path: Path, action: str
    ) -> None:
        backend = FakeBackend(tmp_path / "u")
        backend.install()
        manager = ServiceManager(BIFFD_SPEC, backend, port_probe("free", answers=False))
        with pytest.raises(ServiceActionFailedError, match="does not answer /health"):
            getattr(manager, action)()
        assert backend.config_path().exists()

    def test_a_bad_variable_stops_start_before_it_starts_anything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(VARIABLE, "0")
        backend = FakeBackend(tmp_path / "u")
        backend.install()
        manager = ServiceManager(BIFFD_SPEC, backend, port_probe("free"))
        with pytest.raises(ServiceActionFailedError, match=VARIABLE):
            manager.start()
        assert backend.calls == ["install"]


class TestTheTimeoutSaysWhatTheSupervisorKnows:
    def test_the_error_says_how_long_the_wait_was(self, tmp_path: Path) -> None:
        message = _install_that_times_out(DiagnosableBackend(tmp_path / "u", "state"))
        assert re.search(r"Waited \d+\.\d seconds\.", message)

    def test_it_does_not_claim_how_long_biffd_itself_ran(self, tmp_path: Path) -> None:
        message = _install_that_times_out(DiagnosableBackend(tmp_path / "u", "state"))
        assert "It ran" not in message

    def test_a_service_shown_running_is_slow_or_was_restarted(
        self, tmp_path: Path
    ) -> None:
        message = _install_that_times_out(DiagnosableBackend(tmp_path / "u", "state"))
        assert (
            "shows it running now: it is slow to start, or it exited and was "
            "restarted" in message
        )

    def test_a_service_shown_running_is_not_called_uncrashed(
        self, tmp_path: Path
    ) -> None:
        message = _install_that_times_out(DiagnosableBackend(tmp_path / "u", "state"))
        assert "had not crashed" not in message

    def test_a_service_the_supervisor_does_not_show_running_exited(
        self, tmp_path: Path
    ) -> None:
        backend = DiagnosableBackend(tmp_path / "u", "state", alive=False)
        message = _install_that_times_out(backend)
        assert "does not show it running: it exited or never started" in message

    def test_the_supervisors_own_report_is_in_the_error(self, tmp_path: Path) -> None:
        report = "state = running\nlast exit status = 0"
        message = _install_that_times_out(DiagnosableBackend(tmp_path / "u", report))
        assert "Its report: state = running; last exit status = 0." in message

    def test_the_report_is_read_before_the_rollback_removes_the_unit(
        self, tmp_path: Path
    ) -> None:
        backend = DiagnosableBackend(tmp_path / "u", "state")
        _install_that_times_out(backend)
        assert backend.unit_present_when_asked == [True]

    def test_the_rollback_still_removes_the_unit(self, tmp_path: Path) -> None:
        backend = DiagnosableBackend(tmp_path / "u", "state")
        message = _install_that_times_out(backend)
        assert ("It was removed." in message, backend.config_path().exists()) == (
            True,
            False,
        )

    def test_a_backend_with_no_report_still_says_how_long_it_waited(
        self, tmp_path: Path
    ) -> None:
        message = _install_that_times_out(FakeBackend(tmp_path / "u"))
        assert "Its report" not in message
        assert "Waited" in message

    def test_a_refused_report_is_said_to_be_unavailable(self, tmp_path: Path) -> None:
        class Refusing(DiagnosableBackend):
            __slots__ = ()

            def diagnosis(self) -> str:
                raise ServiceActionFailedError("launchctl print refused")

        message = _install_that_times_out(Refusing(tmp_path / "u", "state"))
        assert "Its report was unavailable (launchctl print refused)" in message

    def test_a_start_timeout_reports_the_same_evidence(self, tmp_path: Path) -> None:
        backend = DiagnosableBackend(tmp_path / "u", "state = running")
        backend.install()
        manager = ServiceManager(BIFFD_SPEC, backend, port_probe("free", answers=False))
        with pytest.raises(ServiceActionFailedError) as caught:
            manager.start()
        assert "Its report: state = running." in str(caught.value)


def test_a_wait_earns_its_one_line_after_three_seconds() -> None:
    assert PortProbe.NOTICE_AFTER_SECONDS == 3.0


class TestTheBackendsReport:
    def test_launchd_prints_the_job_in_the_gui_domain(
        self, script: ScriptedSupervisor
    ) -> None:
        script.answer("launchctl", out="state = running\nlast exit status = 0")
        report = LaunchdBackend(BIFFD_SPEC).diagnosis()
        target = f"gui/{os.getuid()}/{BIFFD_SPEC.launchd_label}"
        assert (report, script.calls) == (
            "state = running\nlast exit status = 0",
            [("launchctl", "print", target)],
        )

    def test_launchd_keeps_the_exit_status_that_prints_late(
        self, script: ScriptedSupervisor
    ) -> None:
        script.answer("launchctl", out=LAUNCHCTL_PRINT)
        report = LaunchdBackend(BIFFD_SPEC).diagnosis().splitlines()
        assert report == [
            "active count = 0",
            "state = not running",
            "runs = 3",
            "last exit code = 78",
        ]

    def test_launchd_falls_back_to_the_first_lines_of_a_report_it_cannot_read(
        self, script: ScriptedSupervisor
    ) -> None:
        script.answer("launchctl", out="\n".join(f"line {i}" for i in range(30)))
        lines = LaunchdBackend(BIFFD_SPEC).diagnosis().splitlines()
        assert lines == [f"line {i}" for i in range(8)]

    def test_systemd_reports_status_of_a_stopped_unit(
        self, script: ScriptedSupervisor
    ) -> None:
        script.answer("systemctl", rc=3, out="biffd.service\n Active: inactive (dead)")
        report = SystemdBackend(BIFFD_SPEC).diagnosis()
        assert (report, script.calls) == (
            "biffd.service\n Active: inactive (dead)",
            [("systemctl", "--user", "status", "--no-pager", "biffd")],
        )

    def test_a_supervisor_that_says_nothing_is_still_a_report(
        self, script: ScriptedSupervisor
    ) -> None:
        script.answer("launchctl", rc=113, err="Could not find service")
        assert LaunchdBackend(BIFFD_SPEC).diagnosis() == "Could not find service"
