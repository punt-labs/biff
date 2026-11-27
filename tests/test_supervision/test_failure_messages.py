"""A failure message is sentences: none runs into the pointer to the log."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from biff.supervision.biffd import BIFFD_SPEC
from biff.supervision.errors import ServiceActionFailedError
from biff.supervision.manager import ServiceManager
from biff.supervision.runner import Supervisor
from tests.test_supervision.fake_backend import FakeBackend, port_probe
from tests.test_supervision.scripted import ScriptedSupervisor

if TYPE_CHECKING:
    from pathlib import Path

# A word, a digit or a closing quote straight before " See" is a run-on.
_RUN_ON = re.compile(r"[^.!?\s] See ")

DETAILS = [
    "",
    "no stop",
    "one stop.",
    "a question?",
    "trailing newline\n",
    "stderr line one\nstderr line two with no stop",
    "stderr line one.\nstderr line two.",
    "ends in a command: biff daemon serve",
]


@pytest.mark.parametrize("detail", DETAILS)
def test_the_pointer_to_the_log_starts_its_own_sentence(detail: str) -> None:
    message = BIFFD_SPEC.failure_message("install", detail)
    assert _RUN_ON.search(message) is None, message


@pytest.mark.parametrize("detail", DETAILS)
def test_the_message_still_names_the_log(detail: str) -> None:
    message = BIFFD_SPEC.failure_message("start", detail)
    assert message.endswith(f"See {BIFFD_SPEC.log_path} for details.")


@pytest.mark.parametrize("stderr", ["refused", "refused.", "refused\n"])
def test_a_refused_supervisor_command_is_not_a_run_on(
    stderr: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ScriptedSupervisor().answer("systemctl", rc=1, err=stderr).install(monkeypatch)
    with pytest.raises(ServiceActionFailedError) as caught:
        Supervisor.require(BIFFD_SPEC, "start", "systemctl", "--user", "start", "x")
    assert _RUN_ON.search(str(caught.value)) is None, str(caught.value)


def test_the_install_timeout_message_is_not_a_run_on(tmp_path: Path) -> None:
    backend = FakeBackend(tmp_path / "biffd.service")
    manager = ServiceManager(BIFFD_SPEC, backend, port_probe("free", answers=False))
    with pytest.raises(ServiceActionFailedError) as caught:
        manager.install()
    assert _RUN_ON.search(str(caught.value)) is None, str(caught.value)
