"""biffd under a real systemd user unit: install, crash, clean stop, uninstall.

Needs a systemd user manager, so ``-m systemd_user`` selects it.  The unit has a
unique name and is removed afterwards.  The daemon itself runs with a throwaway
``HOME``, so its token, port file and log never touch the developer's own.  The
unit file must sit in the real ``~/.config/systemd/user``: the user manager reads
no other directory.
"""

from __future__ import annotations

import dataclasses
import os
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from biff.supervision.biffd import BIFFD_SPEC
from biff.supervision.health import HealthProbe
from biff.supervision.manager import ServiceManager
from biff.supervision.port import PortProbe
from biff.supervision.runner import Supervisor
from biff.supervision.spec import ServiceSpec
from biff.supervision.systemd import SystemdBackend

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.systemd_user

_WAIT_SECONDS: Final = 20.0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _until(condition: object, seconds: float = _WAIT_SECONDS) -> bool:
    """Poll *condition* until it is true or *seconds* pass."""
    assert callable(condition)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return False


def _main_pid(unit: str) -> int:
    shown = Supervisor.run("systemctl", "--user", "show", "-p", "MainPID", unit)
    return int(shown.stdout.strip().partition("=")[2])


@pytest.fixture
def spec(tmp_path: Path) -> Iterator[ServiceSpec]:
    """A uniquely named copy of biffd's spec whose binary runs in a throwaway home."""
    name = f"biffd-it-{uuid.uuid4().hex[:8]}"
    port = _free_port()
    wrapper = Path.home() / ".local" / "bin" / name
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    daemon = Path(sys.executable).parent / "biffd"
    wrapper.write_text(f'#!/bin/sh\nexec env HOME="{tmp_path}" "{daemon}" "$@"\n')
    wrapper.chmod(0o755)
    yield dataclasses.replace(
        BIFFD_SPEC,
        display_name=name,
        systemd_unit=name,
        binary_name=name,
        process_name=name,
        extra_args=("--port", str(port)),
        health_port=port,
    )
    SystemdBackend(dataclasses.replace(BIFFD_SPEC, systemd_unit=name)).uninstall()
    wrapper.unlink(missing_ok=True)


@pytest.fixture
def manager(spec: ServiceSpec) -> ServiceManager:
    return ServiceManager(spec, SystemdBackend(spec), PortProbe(spec.health_port).state)


def test_a_user_manager_is_available() -> None:
    assert SystemdBackend.available()


def test_install_runs_the_real_daemon_under_the_unit(
    spec: ServiceSpec, manager: ServiceManager
) -> None:
    message = manager.install()
    probe = HealthProbe(spec.health_port)
    assert _until(lambda: probe.answers(1.0)), message
    assert SystemdBackend(spec).is_active()


def test_a_crash_is_restarted_and_a_clean_stop_stays_stopped(
    spec: ServiceSpec, manager: ServiceManager
) -> None:
    manager.install()
    probe = HealthProbe(spec.health_port)
    backend = SystemdBackend(spec)
    assert _until(lambda: probe.answers(1.0))
    first = _main_pid(spec.systemd_unit)
    os.kill(first, 9)
    assert _until(lambda: probe.answers(1.0) and _main_pid(spec.systemd_unit) != first)
    manager.stop()
    assert not _until(backend.is_active, seconds=8.0)
    assert not probe.answers(1.0)


def test_start_after_stop_and_restart_replace_the_process(
    spec: ServiceSpec, manager: ServiceManager
) -> None:
    manager.install()
    probe = HealthProbe(spec.health_port)
    assert _until(lambda: probe.answers(1.0))
    manager.stop()
    manager.start()
    assert _until(lambda: probe.answers(1.0))
    before = _main_pid(spec.systemd_unit)
    manager.restart()
    assert _until(lambda: probe.answers(1.0) and _main_pid(spec.systemd_unit) != before)


def test_uninstall_removes_the_unit_and_stops_the_daemon(
    spec: ServiceSpec, manager: ServiceManager
) -> None:
    manager.install()
    probe = HealthProbe(spec.health_port)
    assert _until(lambda: probe.answers(1.0))
    manager.uninstall()
    assert not SystemdBackend(spec).config_path().exists()
    assert _until(lambda: not probe.answers(1.0), seconds=8.0)
