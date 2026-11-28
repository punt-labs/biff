"""Backends report what the supervisor said, and never half-install or half-remove."""

from __future__ import annotations

import dataclasses
import os
import plistlib
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from biff.daemon.handback import Handback
from biff.supervision.biffd import BIFFD_SPEC
from biff.supervision.errors import (
    BinaryNotFoundError,
    PortConflictError,
    ServiceActionFailedError,
)
from biff.supervision.health import HealthProbe, NotBiffdError
from biff.supervision.host import ServiceHost
from biff.supervision.launchd import LaunchdBackend
from biff.supervision.manager import ServiceManager
from biff.supervision.port import PortProbe
from biff.supervision.runner import Supervisor
from biff.supervision.systemd import SystemdBackend
from tests.test_supervision.fake_backend import (
    FailingUninstall,
    FakeBackend,
    port_probe,
)
from tests.test_supervision.scripted import ScriptedSupervisor

if TYPE_CHECKING:
    from collections.abc import Iterator

UNIT = "biffd"
_REAL_DETECT = ServiceHost.detect  # captured before any test patches it
_REAL_LEFTOVER = ServiceHost.leftover_files


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home that holds an executable ``biffd`` and a clean PATH."""
    binary = tmp_path / ".local" / "bin" / "biffd"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    # A CI runner sets XDG_CONFIG_HOME; the unit directory must follow HOME here.
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "py" / "python"))
    return tmp_path


def installed_unit(home: Path) -> Path:
    path = home / ".config" / "systemd" / "user" / "biffd.service"
    path.parent.mkdir(parents=True)
    path.write_text("[Unit]\n")
    return path


class TestSystemdUninstall:
    def test_a_failed_disable_raises_and_keeps_the_unit(
        self, home: Path, script: ScriptedSupervisor
    ) -> None:
        unit = installed_unit(home)
        script.answer("systemctl", "--user", "disable", rc=1, err="Access denied")
        with pytest.raises(ServiceActionFailedError, match="Access denied"):
            SystemdBackend(BIFFD_SPEC).uninstall()
        assert unit.exists()

    def test_a_failed_daemon_reload_raises(
        self, home: Path, script: ScriptedSupervisor
    ) -> None:
        installed_unit(home)
        script.answer("systemctl", "--user", "daemon-reload", rc=1, err="bus is down")
        with pytest.raises(ServiceActionFailedError, match="bus is down"):
            SystemdBackend(BIFFD_SPEC).uninstall()

    def test_a_service_still_active_after_disable_keeps_its_unit(
        self, home: Path, script: ScriptedSupervisor
    ) -> None:
        unit = installed_unit(home)
        script.answer("systemctl", "--user", "is-active", out="active\n")
        with pytest.raises(ServiceActionFailedError, match="still active"):
            SystemdBackend(BIFFD_SPEC).uninstall()
        assert unit.exists()

    def test_a_missing_unit_file_still_stops_an_active_service(
        self, home: Path, script: ScriptedSupervisor
    ) -> None:
        script.answer("systemctl", "--user", "is-active", rc=0, out="active\n")
        script.answer("systemctl", "--user", "stop", rc=1, err="refused")
        with pytest.raises(ServiceActionFailedError, match="refused"):
            SystemdBackend(BIFFD_SPEC).uninstall()
        assert script.ran("systemctl", "--user", "stop", UNIT)

    def test_a_missing_unit_and_an_inactive_service_is_a_no_op(
        self, home: Path, script: ScriptedSupervisor
    ) -> None:
        script.answer("systemctl", "--user", "is-active", rc=3, out="inactive\n")
        SystemdBackend(BIFFD_SPEC).uninstall()
        assert not script.ran("systemctl", "--user", "stop")


class TestSystemdInstall:
    @pytest.mark.parametrize("step", ["daemon-reload", "enable", "restart"])
    def test_a_failed_step_removes_the_unit_and_says_why(
        self, home: Path, script: ScriptedSupervisor, step: str
    ) -> None:
        script.answer("systemctl", "--user", step, rc=1, err="step went wrong")
        backend = SystemdBackend(BIFFD_SPEC)
        with pytest.raises(ServiceActionFailedError, match="step went wrong"):
            backend.install()
        assert not backend.config_path().exists()

    def test_a_failed_enable_is_undone_with_disable(
        self, home: Path, script: ScriptedSupervisor
    ) -> None:
        script.answer("systemctl", "--user", "restart", rc=1, err="nope")
        with pytest.raises(ServiceActionFailedError):
            SystemdBackend(BIFFD_SPEC).install()
        assert script.ran("systemctl", "--user", "disable", "--now", UNIT)

    @pytest.mark.parametrize("verb", ["start", "stop", "restart"])
    def test_a_refused_verb_carries_the_supervisors_stderr(
        self, home: Path, script: ScriptedSupervisor, verb: str
    ) -> None:
        script.answer("systemctl", "--user", verb, rc=1, err="Unit is masked")
        with pytest.raises(ServiceActionFailedError, match="Unit is masked"):
            getattr(SystemdBackend(BIFFD_SPEC), verb)()


class TestSystemdUnit:
    def test_a_crash_loop_is_bounded(self, home: Path) -> None:
        text = SystemdBackend(BIFFD_SPEC).unit_text()
        assert "StartLimitIntervalSec=" in text and "StartLimitBurst=" in text

    def test_systemd_waits_for_the_daemons_whole_stop_before_it_kills_it(
        self, home: Path
    ) -> None:
        # The same ordering as launchd's ExitTimeOut: above the stop, below the
        # limit of the call that waits for it.
        lines = SystemdBackend(BIFFD_SPEC).unit_text().splitlines()
        (given,) = (ln for ln in lines if ln.startswith("TimeoutStopSec="))
        seconds = float(given.partition("=")[2])
        assert Handback.STOP_SECONDS < seconds < Supervisor.STOP_TIMEOUT_SECONDS

    def test_a_permanent_failure_status_is_not_restarted(self, home: Path) -> None:
        text = SystemdBackend(BIFFD_SPEC).unit_text()
        assert f"RestartPreventExitStatus={BIFFD_SPEC.permanent_exit_status}" in text

    def test_exec_start_turns_variable_substitution_off(self, home: Path) -> None:
        assert 'ExecStart=:"' in SystemdBackend(BIFFD_SPEC).unit_text()

    def test_the_unit_passes_the_port_explicitly(self, home: Path) -> None:
        text = SystemdBackend(BIFFD_SPEC).unit_text()
        assert f'"--port" "{BIFFD_SPEC.health_port}"' in text

    def test_percent_and_dollar_in_the_path_are_escaped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        odd = tmp_path / "a%b$c"
        binary = odd / ".local" / "bin" / "biffd"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")
        monkeypatch.setenv("HOME", str(odd))
        text = SystemdBackend(BIFFD_SPEC).unit_text()
        assert "a%%b$c" in text and "$$" not in text


class TestSystemdAvailability:
    def test_a_failing_show_environment_gives_its_stderr_as_the_reason(
        self, script: ScriptedSupervisor
    ) -> None:
        script.answer(
            "systemctl",
            "--user",
            "show-environment",
            rc=1,
            err="Failed to connect to bus",
        )
        assert "Failed to connect to bus" in SystemdBackend.unavailable_reason()

    def test_a_missing_systemctl_is_a_reason_not_a_traceback(
        self, script: ScriptedSupervisor
    ) -> None:
        script.raises("systemctl", error=FileNotFoundError("systemctl"))
        assert SystemdBackend.available() is False

    def test_a_hung_systemctl_is_a_reason_not_a_traceback(
        self, script: ScriptedSupervisor
    ) -> None:
        script.raises("systemctl", error=subprocess.TimeoutExpired("systemctl", 30))
        assert "timed out" in SystemdBackend.unavailable_reason()

    def test_an_unreadable_manager_is_a_reason_not_a_traceback(
        self, script: ScriptedSupervisor
    ) -> None:
        script.raises("systemctl", error=PermissionError("denied"))
        assert "denied" in SystemdBackend.unavailable_reason()


class TestLinger:
    def test_a_hung_loginctl_is_no_warning(
        self, script: ScriptedSupervisor, monkeypatch: pytest.MonkeyPatch, home: Path
    ) -> None:
        monkeypatch.setattr(os, "getlogin", lambda: "u")  # pyright: ignore[reportUnknownLambdaType]
        script.raises("loginctl", error=subprocess.TimeoutExpired("loginctl", 30))
        assert SystemdBackend(BIFFD_SPEC).linger_warning() == ""

    def test_a_failing_loginctl_is_unknown_not_disabled(
        self, script: ScriptedSupervisor, monkeypatch: pytest.MonkeyPatch, home: Path
    ) -> None:
        monkeypatch.setattr(os, "getlogin", lambda: "u")  # pyright: ignore[reportUnknownLambdaType]
        script.answer("loginctl", rc=1, err="no such user")
        assert SystemdBackend(BIFFD_SPEC).linger_warning() == ""

    def test_linger_no_still_warns(
        self, script: ScriptedSupervisor, monkeypatch: pytest.MonkeyPatch, home: Path
    ) -> None:
        monkeypatch.setattr(os, "getlogin", lambda: "u")  # pyright: ignore[reportUnknownLambdaType]
        script.answer("loginctl", out="Linger=no\n")
        assert "enable-linger" in SystemdBackend(BIFFD_SPEC).linger_warning()


class TestHostDetection:
    @pytest.fixture(autouse=True)
    def _linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # conftest replaced detect for every test; these tests need the real one.
        monkeypatch.setattr(ServiceHost, "detect", staticmethod(_REAL_DETECT))
        monkeypatch.setattr("platform.system", lambda: "Linux")  # pyright: ignore[reportUnknownLambdaType]

    def test_a_working_manager_is_found(self, script: ScriptedSupervisor) -> None:
        found = ServiceHost.detect(BIFFD_SPEC)
        assert (found.status, isinstance(found.backend, SystemdBackend)) == (
            "found",
            True,
        )

    def test_no_systemctl_means_absent(self, script: ScriptedSupervisor) -> None:
        script.raises("systemctl", error=FileNotFoundError("systemctl"))
        found = ServiceHost.detect(BIFFD_SPEC)
        assert (found.status, found.backend) == ("absent", None)

    def test_a_manager_that_does_not_answer_is_unreachable_and_says_why(
        self, script: ScriptedSupervisor
    ) -> None:
        script.answer(
            "systemctl",
            "--user",
            "show-environment",
            rc=1,
            err="Failed to connect to bus",
        )
        found = ServiceHost.detect(BIFFD_SPEC)
        assert (found.status, "Failed to connect to bus" in found.reason) == (
            "unreachable",
            True,
        )

    def test_a_hung_manager_is_unreachable_not_a_traceback(
        self, script: ScriptedSupervisor
    ) -> None:
        script.raises("systemctl", error=subprocess.TimeoutExpired("systemctl", 30))
        assert ServiceHost.detect(BIFFD_SPEC).status == "unreachable"


class TestLaunchdPlist:
    def _plist(self, home: Path) -> dict[str, object]:
        return cast(
            "dict[str, object]",
            plistlib.loads(LaunchdBackend(BIFFD_SPEC).plist_bytes()),
        )

    def test_output_goes_to_the_expanded_log_path(self, home: Path) -> None:
        log = str(home / ".punt-labs" / "biff" / "logs" / "biff.log")
        plist = self._plist(home)
        assert (plist["StandardOutPath"], plist["StandardErrorPath"]) == (log, log)

    def test_a_crash_loop_is_throttled(self, home: Path) -> None:
        assert self._plist(home)["ThrottleInterval"] == 30

    def test_launchd_waits_for_the_daemons_whole_stop_before_it_kills_it(
        self, home: Path
    ) -> None:
        # Without ExitTimeOut launchd SIGKILLs biffd after about 20 s; the stop
        # has Handback.STOP_SECONDS, and a restart waits longer than launchd does.
        exit_timeout = self._plist(home)["ExitTimeOut"]
        assert isinstance(exit_timeout, int)
        assert Handback.STOP_SECONDS < exit_timeout < LaunchdBackend.UNLOAD_WAIT_SECONDS

    def test_the_plist_passes_the_port_explicitly(self, home: Path) -> None:
        args = self._plist(home)["ProgramArguments"]
        assert isinstance(args, list) and args[-2:] == [
            "--port",
            str(BIFFD_SPEC.health_port),
        ]


class TestTheSupervisorOutwaitsTheDaemonsStop:
    """The daemon's stop takes up to Handback.STOP_SECONDS; a supervisor call that
    waits for it must not give up first, or install rolls back mid hand-back."""

    LIMIT = Handback.STOP_SECONDS + 15

    def test_the_limit_for_a_stop_exceeds_the_stop_budget(self) -> None:
        assert Supervisor.STOP_TIMEOUT_SECONDS >= self.LIMIT

    @pytest.mark.parametrize("verb", ["stop", "restart"])
    def test_systemctl_stop_and_restart(
        self, home: Path, script: ScriptedSupervisor, verb: str
    ) -> None:
        backend = SystemdBackend(BIFFD_SPEC)
        getattr(backend, verb)()
        limit = script.timeout_of("systemctl", "--user", verb)
        assert limit >= self.LIMIT

    def test_systemctl_restart_inside_install(
        self, home: Path, script: ScriptedSupervisor
    ) -> None:
        SystemdBackend(BIFFD_SPEC).install()
        limit = script.timeout_of("systemctl", "--user", "restart")
        assert limit >= self.LIMIT

    def test_systemctl_disable_now_inside_uninstall(
        self, home: Path, script: ScriptedSupervisor
    ) -> None:
        installed_unit(home)
        script.answer("systemctl", "--user", "is-active", rc=3, out="inactive\n")
        SystemdBackend(BIFFD_SPEC).uninstall()
        limit = script.timeout_of("systemctl", "--user", "disable", "--now")
        assert limit >= self.LIMIT

    def test_launchctl_bootout(self, home: Path, script: ScriptedSupervisor) -> None:
        script.answer_in_turn("launchctl", "print", rcs=[0, 113])
        LaunchdBackend(BIFFD_SPEC).stop()
        limit = script.timeout_of("launchctl", "bootout")
        assert limit >= self.LIMIT


class TestLaunchd:
    @pytest.fixture
    def plist(self, home: Path) -> Path:
        path = home / "Library" / "LaunchAgents" / "com.punt-labs.biffd.plist"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"old")
        return path

    @pytest.fixture(autouse=True)
    def _fast_unload_poll(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(LaunchdBackend, "UNLOAD_POLL_SECONDS", 0.01)

    def test_a_failed_bootout_raises_and_keeps_the_plist(
        self, script: ScriptedSupervisor, plist: Path
    ) -> None:
        script.answer("launchctl", "bootout", rc=5, err="Operation not permitted")
        with pytest.raises(ServiceActionFailedError, match="Operation not permitted"):
            LaunchdBackend(BIFFD_SPEC).uninstall()
        assert plist.exists()

    def test_a_job_still_loaded_after_bootout_keeps_the_plist(
        self, script: ScriptedSupervisor, plist: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(LaunchdBackend, "UNLOAD_WAIT_SECONDS", 0.05)
        with pytest.raises(ServiceActionFailedError, match="still loaded"):
            LaunchdBackend(BIFFD_SPEC).uninstall()
        assert plist.exists()

    def test_a_failed_bootstrap_removes_the_new_plist_and_says_why(
        self, script: ScriptedSupervisor, plist: Path
    ) -> None:
        script.answer("launchctl", "print", rc=113)
        script.answer("launchctl", "bootstrap", rc=5, err="Input/output error")
        backend = LaunchdBackend(BIFFD_SPEC)
        with pytest.raises(ServiceActionFailedError, match="Input/output error"):
            backend.install()
        assert not plist.exists()

    def test_restart_stops_gracefully_waits_then_bootstraps(
        self, script: ScriptedSupervisor, plist: Path
    ) -> None:
        script.answer_in_turn("launchctl", "print", rcs=[0, 0, 0, 113])
        assert LaunchdBackend(BIFFD_SPEC).restart() is True
        assert not script.ran("launchctl", "kickstart", "-k")
        assert script.index("launchctl", "bootout") < script.index(
            "launchctl", "bootstrap"
        )

    def test_bootstrap_waits_for_the_unload_to_finish(
        self, script: ScriptedSupervisor, plist: Path
    ) -> None:
        script.answer_in_turn("launchctl", "print", rcs=[0, 0, 0, 113])
        LaunchdBackend(BIFFD_SPEC).restart()
        last_print = script.index("launchctl", "print")
        assert last_print < script.index("launchctl", "bootstrap")

    def test_a_refused_start_carries_stderr(
        self, script: ScriptedSupervisor, plist: Path
    ) -> None:
        script.answer("launchctl", "print", rc=113)
        script.answer("launchctl", "bootstrap", rc=5, err="Bootstrap failed: 5")
        with pytest.raises(ServiceActionFailedError, match="Bootstrap failed"):
            LaunchdBackend(BIFFD_SPEC).start()


class TestSpecBinary:
    def test_a_binary_on_path_resolves_when_not_in_local_bin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bin_dir = tmp_path / "pipx" / "bin"
        bin_dir.mkdir(parents=True)
        binary = bin_dir / "biffd"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
        monkeypatch.setenv("PATH", str(bin_dir))
        spec = dataclasses.replace(BIFFD_SPEC, beside_interpreter=False)
        assert spec.resolve_exec_args()[0] == str(binary)

    def test_a_missing_binary_names_both_places(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
        spec = dataclasses.replace(BIFFD_SPEC, beside_interpreter=False)
        with pytest.raises(BinaryNotFoundError, match="PATH"):
            spec.resolve_exec_args()

    def test_the_spec_passes_the_port_to_biffd(self) -> None:
        assert BIFFD_SPEC.extra_args == ("--port", str(BIFFD_SPEC.health_port))


class TestManagerInstallProof:
    def test_a_daemon_that_never_answers_fails_the_install(
        self, tmp_path: Path, home: Path
    ) -> None:
        backend = FakeBackend(tmp_path / "biffd.service")
        manager = ServiceManager(BIFFD_SPEC, backend, port_probe("free", answers=False))
        with pytest.raises(ServiceActionFailedError, match="does not answer"):
            manager.install()

    def test_the_failure_names_the_diagnosis(self, tmp_path: Path, home: Path) -> None:
        manager = ServiceManager(
            BIFFD_SPEC,
            FakeBackend(tmp_path / "biffd.service"),
            port_probe("free", answers=False),
        )
        with pytest.raises(ServiceActionFailedError, match=r"biff daemon serve"):
            manager.install()

    def test_a_failed_health_check_removes_the_registration(
        self, tmp_path: Path, home: Path
    ) -> None:
        backend = FakeBackend(tmp_path / "biffd.service")
        manager = ServiceManager(BIFFD_SPEC, backend, port_probe("free", answers=False))
        with pytest.raises(ServiceActionFailedError, match="It was removed"):
            manager.install()
        assert (backend.calls, backend.config_path().exists()) == (
            ["install", "uninstall"],
            False,
        )

    def test_a_removal_that_fails_too_is_reported(
        self, tmp_path: Path, home: Path
    ) -> None:
        backend = FailingUninstall(tmp_path / "biffd.service")
        manager = ServiceManager(BIFFD_SPEC, backend, port_probe("free", answers=False))
        with pytest.raises(ServiceActionFailedError, match="Removing it failed too"):
            manager.install()

    def test_an_unresponsive_port_does_not_block_install(
        self, tmp_path: Path, home: Path
    ) -> None:
        backend = FakeBackend(tmp_path / "biffd.service")
        ServiceManager(BIFFD_SPEC, backend, port_probe("unresponsive")).install()
        assert backend.calls == ["install"]

    def test_only_a_foreign_listener_is_a_conflict(
        self, tmp_path: Path, home: Path
    ) -> None:
        manager = ServiceManager(
            BIFFD_SPEC, FakeBackend(tmp_path / "u"), port_probe("foreign")
        )
        with pytest.raises(PortConflictError):
            manager.install()


class _NotBiffd(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<html>nope</html>")

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def foreign_port() -> Iterator[int]:
    server = HTTPServer(("127.0.0.1", 0), _NotBiffd)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.fixture
def silent_port() -> Iterator[int]:
    """A listener that accepts connections and never answers."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    yield sock.getsockname()[1]
    sock.close()


class TestPortProbe:
    def test_a_clear_non_biffd_answer_is_foreign(self, foreign_port: int) -> None:
        assert PortProbe(foreign_port).state() == "foreign"

    def test_a_listener_that_never_answers_is_unresponsive(
        self, silent_port: int
    ) -> None:
        assert PortProbe(silent_port).state() == "unresponsive"

    def test_a_non_biffd_answer_raises_the_specific_error(
        self, foreign_port: int
    ) -> None:
        with pytest.raises(NotBiffdError):
            HealthProbe(foreign_port).fetch(1.0)

    def test_a_timeout_is_not_a_non_biffd_answer(self, silent_port: int) -> None:
        with pytest.raises(ConnectionError) as caught:
            HealthProbe(silent_port).fetch(0.3)
        assert not isinstance(caught.value, NotBiffdError)

    def test_waiting_gives_up_after_the_deadline(self) -> None:
        assert PortProbe(1).wait_until_answering(0.3) is False


class TestInstallationLookup:
    """biffd is found where this installation put it, not only in ~/.local/bin."""

    def test_the_interpreters_own_bin_directory_is_searched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        venv_bin = tmp_path / "envs" / "biff" / "bin"
        venv_bin.mkdir(parents=True)
        binary = venv_bin / "biffd"
        binary.write_text("#!/bin/sh\n")
        monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
        monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
        monkeypatch.setattr(sys, "executable", str(venv_bin / "python"))
        assert BIFFD_SPEC.resolve_exec_args()[0] == str(binary)

    def test_the_stable_uv_tool_link_still_comes_first(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        venv_bin = home / "envs" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "biffd").write_text("#!/bin/sh\n")
        monkeypatch.setattr(sys, "executable", str(venv_bin / "python"))
        assert BIFFD_SPEC.resolve_exec_args()[0] == str(home / ".local/bin/biffd")

    def test_a_spec_that_does_not_look_beside_the_interpreter_does_not(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        venv_bin = tmp_path / "bin"
        venv_bin.mkdir()
        (venv_bin / "biffd").write_text("#!/bin/sh\n")
        monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
        monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
        monkeypatch.setattr(sys, "executable", str(venv_bin / "python"))
        spec = dataclasses.replace(BIFFD_SPEC, beside_interpreter=False)
        with pytest.raises(BinaryNotFoundError):
            spec.resolve_exec_args()


class TestUnitDirectory:
    def test_the_unit_follows_xdg_config_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        path = SystemdBackend(BIFFD_SPEC).config_path()
        assert path == tmp_path / "xdg" / "systemd" / "user" / "biffd.service"

    def test_without_xdg_config_home_it_is_dot_config(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        assert SystemdBackend(BIFFD_SPEC).config_path() == (
            home / ".config" / "systemd" / "user" / "biffd.service"
        )


class TestLaunchdNeedsAGuiSession:
    @pytest.fixture(autouse=True)
    def _mac(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ServiceHost, "detect", staticmethod(_REAL_DETECT))
        monkeypatch.setattr("platform.system", lambda: "Darwin")  # pyright: ignore[reportUnknownLambdaType]

    def test_a_reachable_gui_domain_is_found(self, script: ScriptedSupervisor) -> None:
        found = ServiceHost.detect(BIFFD_SPEC)
        assert (found.status, isinstance(found.backend, LaunchdBackend)) == (
            "found",
            True,
        )

    def test_no_gui_domain_is_unreachable_and_says_so(
        self, script: ScriptedSupervisor
    ) -> None:
        script.answer("launchctl", "print", rc=113, err="Could not find domain")
        found = ServiceHost.detect(BIFFD_SPEC)
        assert (found.status, "no GUI session" in found.reason) == ("unreachable", True)
        assert found.backend is None

    def test_no_launchctl_is_absent(self, script: ScriptedSupervisor) -> None:
        script.raises("launchctl", error=FileNotFoundError("launchctl"))
        assert ServiceHost.detect(BIFFD_SPEC).status == "absent"


class TestLeftoverFiles:
    """A registration an earlier login made is found without asking the manager."""

    def test_the_unit_file_is_found_on_linux(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("platform.system", lambda: "Linux")  # pyright: ignore[reportUnknownLambdaType]
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        unit = installed_unit(home)
        monkeypatch.setattr(ServiceHost, "leftover_files", staticmethod(_REAL_LEFTOVER))
        assert ServiceHost.leftover_files(BIFFD_SPEC) == (unit,)

    def test_nothing_is_found_when_nothing_is_installed(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("platform.system", lambda: "Linux")  # pyright: ignore[reportUnknownLambdaType]
        monkeypatch.setattr(ServiceHost, "leftover_files", staticmethod(_REAL_LEFTOVER))
        assert ServiceHost.leftover_files(BIFFD_SPEC) == ()


class TestInstallScript:
    """The bootstrap's service step, run with a stub in place of ``biff``."""

    SCRIPT = Path(__file__).parents[2] / "install.sh"
    SERVICE_CALL = '"$BINARY" daemon install < /dev/null ||'
    PLUGIN_CALL = 'claude plugin install "${PLUGIN_NAME}'
    CLI_CALL = 'uv tool install --force $PYTHON_FLAG "$PACKAGE==$VERSION"'
    VERIFY_STEP = "# --- Step 7: Verify"

    @classmethod
    def order_is_right(cls, text: str) -> bool:
        """Return whether the CLI install, the service call and the plugin run so.

        The service runs the binary the CLI install provides, and the plugin step
        must not run before a service failure can be reported.
        """
        return (
            text.index(cls.CLI_CALL)
            < text.index(cls.SERVICE_CALL)
            < text.index(cls.PLUGIN_CALL)
        )

    @classmethod
    def _moved(cls, text: str, *, before: str) -> str:
        """Return *text* with the service call cut out and put in front of *before*."""
        without = text.replace(cls.SERVICE_CALL, "", 1)
        at = without.index(before)
        return without[:at] + cls.SERVICE_CALL + without[at:]

    def _blocks(self) -> tuple[str, str]:
        text = self.SCRIPT.read_text()

        def block(name: str) -> str:
            start = text.index(f"# >>> {name}")
            return text[start : text.index(f"# <<< {name}")]

        return block("biffd service"), block("service verdict")

    def _run(
        self,
        tmp_path: Path,
        *,
        help_status: int,
        help_error: str = "",
        install_status: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        stub = tmp_path / "biff"
        stub.write_text(
            "#!/bin/sh\n"
            f'if [ "$3" = "--help" ]; then echo "{help_error}" >&2; '
            f"exit {help_status}; fi\n"
            f"exit {install_status}\n"
        )
        stub.chmod(0o755)
        service, verdict = self._blocks()
        program = (
            'info() { echo "info: $1"; }\n'
            'ok() { echo "ok: $1"; }\n'
            'warn() { echo "warn: $1"; }\n'
            'fail() { echo "fail: $1"; exit 1; }\n'
            f'BINARY="{stub}"\n{service}\necho plugin-step-ran\n{verdict}\n'
        )
        return Supervisor.run("/bin/sh", "-c", program)

    def test_the_cli_install_then_the_service_then_the_plugin(self) -> None:
        assert self.order_is_right(self.SCRIPT.read_text())

    def test_the_order_check_fails_when_the_service_moves_after_the_plugin(
        self,
    ) -> None:
        mutant = self._moved(self.SCRIPT.read_text(), before=self.VERIFY_STEP)
        assert not self.order_is_right(mutant)

    def test_the_order_check_fails_when_the_service_moves_before_the_cli_install(
        self,
    ) -> None:
        mutant = self._moved(self.SCRIPT.read_text(), before=self.CLI_CALL)
        assert not self.order_is_right(mutant)

    def test_a_still_starting_service_is_not_called_unsupervised(
        self, tmp_path: Path
    ) -> None:
        done = self._run(tmp_path, help_status=0, install_status=4)
        assert (done.returncode, "no service manager" in done.stdout) == (0, False)
        assert "still starting" in done.stdout

    @pytest.mark.parametrize("status", [0, 3])
    def test_installed_or_no_manager_succeeds(
        self, tmp_path: Path, status: int
    ) -> None:
        done = self._run(tmp_path, help_status=0, install_status=status)
        assert (done.returncode, "plugin-step-ran" in done.stdout) == (0, True)

    def test_a_refusing_manager_fails_after_the_plugin_step(
        self, tmp_path: Path
    ) -> None:
        done = self._run(tmp_path, help_status=0, install_status=1)
        assert done.returncode == 1
        assert done.stdout.index("plugin-step-ran") < done.stdout.index("fail:")

    def test_a_release_without_the_command_is_skipped_with_a_note(
        self, tmp_path: Path
    ) -> None:
        done = self._run(
            tmp_path, help_status=2, help_error="Error: No such command 'install'."
        )
        assert (done.returncode, "no 'daemon install'" in done.stdout) == (0, True)

    def test_a_cli_that_fails_to_start_fails_loudly_with_its_stderr(
        self, tmp_path: Path
    ) -> None:
        done = self._run(
            tmp_path, help_status=1, help_error="ImportError: no module named nats"
        )
        assert done.returncode == 1
        assert "ImportError: no module named nats" in done.stdout
        assert "plugin-step-ran" in done.stdout
