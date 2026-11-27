"""Naming the program that holds biffd's port, as far as the host can say."""

from __future__ import annotations

import os
import socket
import subprocess
from typing import TYPE_CHECKING

import pytest

from biff.supervision.biffd import BIFFD_SPEC
from biff.supervision.errors import PortConflictError
from biff.supervision.holder import Holder, PortHolder
from biff.supervision.manager import ServiceManager
from tests.test_supervision.fake_backend import FakeBackend, port_probe

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from tests.test_supervision.scripted import ScriptedSupervisor

LSOF = ("lsof", "-nP", "-iTCP:8424", "-sTCP:LISTEN", "-Fpcn")
SS_LINE = 'LISTEN 0 5 127.0.0.1:8424 0.0.0.0:* users:(("python3",pid=4930,fd=3))'


class TestLsof:
    def test_names_the_command_and_pid(self, script: ScriptedSupervisor) -> None:
        script.answer("lsof", out="p4930\ncPython\nn127.0.0.1:8424\n")
        assert PortHolder.find(8424) == Holder("Python", 4930)

    def test_asks_lsof_for_that_port_with_no_shell(
        self, script: ScriptedSupervisor
    ) -> None:
        script.answer("lsof", out="p1\ncx\n")
        PortHolder.find(8424)
        assert script.calls == [LSOF]

    def test_the_lookup_has_a_two_second_limit(
        self, script: ScriptedSupervisor
    ) -> None:
        script.answer("lsof", out="p1\ncx\n")
        PortHolder.find(8424)
        assert script.timeouts == [2.0]

    def test_the_first_process_wins(self, script: ScriptedSupervisor) -> None:
        script.answer("lsof", out="p10\ncfirst\nn*:8424\np11\ncsecond\nn*:8424\n")
        assert PortHolder.find(8424) == Holder("first", 10)

    def test_a_command_with_a_space_is_kept_whole(
        self, script: ScriptedSupervisor
    ) -> None:
        script.answer("lsof", out="p7\ncGoogle Chrome\nn*:8424\n")
        assert PortHolder.find(8424) == Holder("Google Chrome", 7)


class TestFallbackToSs:
    def test_ss_is_asked_when_lsof_is_not_installed(
        self, script: ScriptedSupervisor
    ) -> None:
        script.raises("lsof", error=FileNotFoundError("lsof"))
        script.answer("ss", out=SS_LINE)
        assert PortHolder.find(8424) == Holder("python3", 4930)

    def test_ss_is_asked_when_lsof_finds_nothing(
        self, script: ScriptedSupervisor
    ) -> None:
        script.answer("lsof", rc=1)
        script.answer("ss", out=SS_LINE)
        assert PortHolder.find(8424) == Holder("python3", 4930)

    def test_ss_is_asked_for_that_port_with_no_shell(
        self, script: ScriptedSupervisor
    ) -> None:
        script.raises("lsof", error=FileNotFoundError("lsof"))
        script.answer("ss", out=SS_LINE)
        PortHolder.find(8424)
        assert script.calls[-1] == ("ss", "-ltnpH", "sport = :8424")


class TestNeverGuessesAndNeverFails:
    def test_no_tool_at_all_is_no_holder(self, script: ScriptedSupervisor) -> None:
        script.raises("lsof", error=FileNotFoundError("lsof"))
        script.raises("ss", error=FileNotFoundError("ss"))
        assert PortHolder.find(8424) is None

    def test_output_that_names_nothing_is_no_holder(
        self, script: ScriptedSupervisor
    ) -> None:
        script.answer("lsof", out="")
        script.answer("ss", out="")
        assert PortHolder.find(8424) is None

    def test_a_pid_without_a_command_is_no_holder(
        self, script: ScriptedSupervisor
    ) -> None:
        script.answer("lsof", out="p4930\n")
        script.answer("ss", out="")
        assert PortHolder.find(8424) is None

    def test_a_hung_lookup_is_no_holder(self, script: ScriptedSupervisor) -> None:
        script.raises("lsof", error=subprocess.TimeoutExpired("lsof", 2.0))
        script.raises("ss", error=subprocess.TimeoutExpired("ss", 2.0))
        assert PortHolder.find(8424) is None

    def test_a_refused_lookup_is_no_holder(self, script: ScriptedSupervisor) -> None:
        script.raises("lsof", error=PermissionError("denied"))
        script.raises("ss", error=PermissionError("denied"))
        assert PortHolder.find(8424) is None


class TestARealListener:
    @pytest.fixture
    def listener(self) -> Iterator[int]:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            yield int(sock.getsockname()[1])

    def test_names_this_process(self, listener: int) -> None:
        holder = PortHolder.find(listener)
        assert holder is not None
        assert holder.pid == os.getpid()


class TestTheRefusalNamesTheHolder:
    def test_a_foreign_listener_is_named(self, tmp_path: Path) -> None:
        port = port_probe("foreign", holder=Holder("python3", 4930))
        manager = ServiceManager(BIFFD_SPEC, FakeBackend(tmp_path / "u"), port)
        with pytest.raises(PortConflictError, match=r"held by python3 \(pid 4930\)"):
            manager.install()

    def test_the_lsof_hint_stays(self, tmp_path: Path) -> None:
        port = port_probe("foreign", holder=Holder("python3", 4930))
        manager = ServiceManager(BIFFD_SPEC, FakeBackend(tmp_path / "u"), port)
        with pytest.raises(PortConflictError, match="lsof -nP -iTCP:8424"):
            manager.install()

    def test_an_unnamed_holder_adds_nothing(self, tmp_path: Path) -> None:
        port = port_probe("foreign", holder=None)
        manager = ServiceManager(BIFFD_SPEC, FakeBackend(tmp_path / "u"), port)
        with pytest.raises(PortConflictError) as caught:
            manager.install()
        assert "pid" not in str(caught.value)

    @pytest.mark.parametrize("action", ["start", "restart"])
    def test_start_and_restart_name_it_too(self, tmp_path: Path, action: str) -> None:
        backend = FakeBackend(tmp_path / "u")
        backend.install()
        port = port_probe("foreign", holder=Holder("python3", 4930))
        manager = ServiceManager(BIFFD_SPEC, backend, port)
        with pytest.raises(PortConflictError, match=r"python3 \(pid 4930\)"):
            getattr(manager, action)()
