"""A biffd that is running and still starting is not a failed start."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from biff.supervision.biffd import BIFFD_SPEC
from biff.supervision.errors import ServiceActionFailedError, ServiceStillStartingError
from biff.supervision.launchd import LaunchdBackend
from biff.supervision.manager import ServiceManager
from biff.supervision.systemd import SystemdBackend
from tests.test_supervision.fake_backend import PiddedBackend, port_probe
from tests.test_supervision.test_launchd_report import RUNNING_JOB

if TYPE_CHECKING:
    from pathlib import Path

    from tests.test_supervision.scripted import ScriptedSupervisor

PID = 70860


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home whose biff.log is where biffd's own startup line would be."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(os, "getloadavg", lambda: (7.5, 6.0, 5.0))
    return tmp_path


def write_log(home: Path, *lines: str) -> None:
    log = home / ".punt-labs" / "biff" / "logs" / "biff.log"
    log.parent.mkdir(parents=True)
    log.write_text("\n".join(lines) + "\n")


def startup_line(pid: int) -> str:
    return (
        f"2026-11-27 00:35:20 [INFO] biff.daemon.foreground: biffd starting: "
        f"version=1.26.0 pid={pid} port=8424 run_dir=~/.punt-labs/biff/run python=~/p"
    )


def install_with(backend: PiddedBackend) -> BaseException:
    manager = ServiceManager(BIFFD_SPEC, backend, port_probe("free", answers=False))
    with pytest.raises((ServiceStillStartingError, ServiceActionFailedError)) as caught:
        manager.install()
    return caught.value


class TestFourCombinations:
    def test_running_with_its_startup_line_is_still_starting(
        self, home: Path, tmp_path: Path
    ) -> None:
        write_log(home, startup_line(PID))
        backend = PiddedBackend(tmp_path / "u", pid=PID, alive=True)
        assert isinstance(install_with(backend), ServiceStillStartingError)

    def test_running_without_a_startup_line_is_a_failure(
        self, home: Path, tmp_path: Path
    ) -> None:
        write_log(home, "2026-11-27 00:35:20 [INFO] something else")
        backend = PiddedBackend(tmp_path / "u", pid=PID, alive=True)
        assert isinstance(install_with(backend), ServiceActionFailedError)

    def test_not_running_with_a_startup_line_is_a_failure(
        self, home: Path, tmp_path: Path
    ) -> None:
        write_log(home, startup_line(PID))
        backend = PiddedBackend(tmp_path / "u", pid=PID, alive=False)
        assert isinstance(install_with(backend), ServiceActionFailedError)

    def test_not_running_without_a_startup_line_is_a_failure(
        self, home: Path, tmp_path: Path
    ) -> None:
        write_log(home, "2026-11-27 00:35:20 [INFO] something else")
        backend = PiddedBackend(tmp_path / "u", pid=PID, alive=False)
        assert isinstance(install_with(backend), ServiceActionFailedError)

    def test_another_pids_startup_line_does_not_count(
        self, home: Path, tmp_path: Path
    ) -> None:
        write_log(home, startup_line(PID + 1))
        backend = PiddedBackend(tmp_path / "u", pid=PID, alive=True)
        assert isinstance(install_with(backend), ServiceActionFailedError)

    def test_a_pid_the_supervisor_does_not_know_is_a_failure(
        self, home: Path, tmp_path: Path
    ) -> None:
        write_log(home, startup_line(PID))
        backend = PiddedBackend(tmp_path / "u", pid=None, alive=True)
        assert isinstance(install_with(backend), ServiceActionFailedError)

    def test_no_log_at_all_is_a_failure(self, home: Path, tmp_path: Path) -> None:
        backend = PiddedBackend(tmp_path / "u", pid=PID, alive=True)
        assert isinstance(install_with(backend), ServiceActionFailedError)


class TestWhatInstallDoes:
    def test_a_still_starting_install_keeps_the_service(
        self, home: Path, tmp_path: Path
    ) -> None:
        write_log(home, startup_line(PID))
        backend = PiddedBackend(tmp_path / "u", pid=PID, alive=True)
        install_with(backend)
        assert (backend.calls, backend.config_path().exists()) == (["install"], True)

    def test_a_failed_install_still_rolls_back(
        self, home: Path, tmp_path: Path
    ) -> None:
        backend = PiddedBackend(tmp_path / "u", pid=PID, alive=False)
        install_with(backend)
        assert (backend.calls, backend.config_path().exists()) == (
            ["install", "uninstall"],
            False,
        )

    def test_the_message_names_the_wait_the_load_and_that_it_is_alive(
        self, home: Path, tmp_path: Path
    ) -> None:
        write_log(home, startup_line(PID))
        message = str(install_with(PiddedBackend(tmp_path / "u", pid=PID)))
        assert [
            part
            for part in (
                "still starting after 30 seconds",
                "machine load average 7.5",
                "it is running, not crashed",
                "left installed",
            )
            if part not in message
        ] == []


class TestWhatStartAndRestartDo:
    @pytest.mark.parametrize("action", ["start", "restart"])
    def test_a_still_starting_service_is_reported_not_failed(
        self, home: Path, tmp_path: Path, action: str
    ) -> None:
        write_log(home, startup_line(PID))
        backend = PiddedBackend(tmp_path / "u", pid=PID)
        backend.install()
        manager = ServiceManager(BIFFD_SPEC, backend, port_probe("free", answers=False))
        with pytest.raises(ServiceStillStartingError, match="machine load average"):
            getattr(manager, action)()
        assert backend.config_path().exists()

    @pytest.mark.parametrize("action", ["start", "restart"])
    def test_a_dead_service_is_still_a_failure(
        self, home: Path, tmp_path: Path, action: str
    ) -> None:
        backend = PiddedBackend(tmp_path / "u", pid=None, alive=False)
        backend.install()
        manager = ServiceManager(BIFFD_SPEC, backend, port_probe("free", answers=False))
        with pytest.raises(ServiceActionFailedError):
            getattr(manager, action)()


class TestBackendsKnowTheirPid:
    def test_launchd_reads_the_pid_of_the_job_not_of_a_nested_block(
        self, script: ScriptedSupervisor
    ) -> None:
        script.answer("launchctl", out=RUNNING_JOB)
        assert LaunchdBackend(BIFFD_SPEC).main_pid() == PID

    def test_launchd_has_no_pid_for_a_job_that_is_not_running(
        self, script: ScriptedSupervisor
    ) -> None:
        script.answer("launchctl", out=RUNNING_JOB.replace("\tpid = 70860\n", ""))
        assert LaunchdBackend(BIFFD_SPEC).main_pid() is None

    def test_systemd_reads_main_pid(self, script: ScriptedSupervisor) -> None:
        script.answer("systemctl", out="1234\n")
        assert SystemdBackend(BIFFD_SPEC).main_pid() == 1234
        assert script.calls == [
            ("systemctl", "--user", "show", "-p", "MainPID", "--value", "biffd")
        ]

    def test_systemd_main_pid_zero_means_not_running(
        self, script: ScriptedSupervisor
    ) -> None:
        script.answer("systemctl", out="0\n")
        assert SystemdBackend(BIFFD_SPEC).main_pid() is None
