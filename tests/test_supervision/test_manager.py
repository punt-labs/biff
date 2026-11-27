"""The biffd service spec and the manager that drives a backend with it."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from biff.supervision.biffd import BIFFD_SPEC
from biff.supervision.errors import (
    BinaryNotFoundError,
    PortConflictError,
    ServiceActionFailedError,
    ServiceNotInstalledError,
)
from biff.supervision.manager import ServiceManager
from tests.test_supervision.fake_backend import FakeBackend, port_probe

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def unit(tmp_path: Path) -> Path:
    return tmp_path / "biffd.service"


@pytest.fixture
def home_with_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home that holds a ``biffd`` executable at the stable install path."""
    binary = tmp_path / ".local" / "bin" / "biffd"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


class TestSpec:
    def test_names_match_the_design_table(self) -> None:
        assert (
            BIFFD_SPEC.launchd_label,
            BIFFD_SPEC.systemd_unit,
            BIFFD_SPEC.binary_name,
            BIFFD_SPEC.cli_verb,
            BIFFD_SPEC.cli_name,
        ) == ("com.punt-labs.biffd", "biffd", "biffd", "daemon", "biff")

    def test_guards_port_8424(self) -> None:
        assert BIFFD_SPEC.health_port == 8424

    def test_a_clean_stop_stays_stopped(self) -> None:
        assert BIFFD_SPEC.restart_on_crash_only is True

    def test_logs_to_the_file_every_biff_process_writes(self) -> None:
        assert BIFFD_SPEC.log_path == "~/.punt-labs/biff/logs/biff.log"

    def test_failure_message_points_at_the_log(self) -> None:
        assert BIFFD_SPEC.failure_message("start") == (
            "biffd start failed. See ~/.punt-labs/biff/logs/biff.log for details."
        )

    def test_exec_args_resolve_the_installed_binary(
        self, home_with_binary: Path
    ) -> None:
        assert BIFFD_SPEC.resolve_exec_args() == [
            str(home_with_binary / ".local" / "bin" / "biffd")
        ]

    def test_exec_args_name_the_missing_binary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        with pytest.raises(BinaryNotFoundError, match="biffd"):
            BIFFD_SPEC.resolve_exec_args()


class TestManager:
    def test_install_registers_and_reports_running(
        self, unit: Path, home_with_binary: Path
    ) -> None:
        backend = FakeBackend(unit)
        message = ServiceManager(BIFFD_SPEC, backend, port_probe("free")).install()
        assert ("biffd running." in message, backend.calls) == (True, ["install"])

    def test_install_names_the_unit_and_command(
        self, unit: Path, home_with_binary: Path
    ) -> None:
        message = ServiceManager(
            BIFFD_SPEC, FakeBackend(unit), port_probe("free")
        ).install()
        assert (f"Service: {unit}" in message, "Command: " in message) == (True, True)

    def test_install_over_a_foreign_listener_is_refused(
        self, unit: Path, home_with_binary: Path
    ) -> None:
        backend = FakeBackend(unit)
        manager = ServiceManager(BIFFD_SPEC, backend, port_probe("foreign"))
        with pytest.raises(PortConflictError, match="8424"):
            manager.install()
        assert backend.calls == []

    def test_install_over_a_running_biffd_is_allowed(
        self, unit: Path, home_with_binary: Path
    ) -> None:
        backend = FakeBackend(unit)
        ServiceManager(BIFFD_SPEC, backend, port_probe("ours")).install()
        assert backend.calls == ["install"]

    def test_uninstall_removes_the_unit(self, unit: Path) -> None:
        unit.write_text("unit")
        message = ServiceManager(
            BIFFD_SPEC, FakeBackend(unit), port_probe("free")
        ).uninstall()
        assert (unit.exists(), f"Removed {unit}" in message) == (False, True)

    def test_start_requires_an_installed_unit(self, unit: Path) -> None:
        manager = ServiceManager(BIFFD_SPEC, FakeBackend(unit), port_probe("free"))
        with pytest.raises(ServiceNotInstalledError, match="biff daemon install"):
            manager.start()

    def test_a_refused_start_names_the_log(self, unit: Path) -> None:
        unit.write_text("unit")
        manager = ServiceManager(
            BIFFD_SPEC, FakeBackend(unit, ok=False), port_probe("free")
        )
        with pytest.raises(ServiceActionFailedError, match=r"biff\.log"):
            manager.start()

    def test_a_refused_restart_names_the_log(self, unit: Path) -> None:
        unit.write_text("unit")
        manager = ServiceManager(
            BIFFD_SPEC, FakeBackend(unit, ok=False), port_probe("free")
        )
        with pytest.raises(ServiceActionFailedError, match="restart failed"):
            manager.restart()

    def test_stop_failure_is_reported(self, unit: Path) -> None:
        manager = ServiceManager(
            BIFFD_SPEC, FakeBackend(unit, ok=False), port_probe("free")
        )
        with pytest.raises(ServiceActionFailedError, match="stop failed"):
            manager.stop()

    def test_start_then_stop_reach_the_backend_in_order(self, unit: Path) -> None:
        unit.write_text("unit")
        backend = FakeBackend(unit)
        manager = ServiceManager(BIFFD_SPEC, backend, port_probe("free"))
        manager.start()
        manager.stop()
        assert backend.calls == ["start", "stop"]
