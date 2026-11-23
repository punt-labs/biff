"""The launch-flag gate: was Claude Code started with a channel flag (DES-087)."""

from __future__ import annotations

import os

import psutil
import pytest

from biff.plugins.claude_code.launch_flag import LaunchFlagGate
from biff.plugins.claude_code.startup_warning import StartupWarning

POLL = "Run /biff:read 2m to poll."
ENV = {"CLAUDE_PID": "4242"}


def _warning(
    env: dict[str, str], argv: list[str] | Exception
) -> tuple[str | None, list[int]]:
    """Return the launch-flag warning and the pids whose argv was read."""
    asked: list[int] = []

    def read(pid: int) -> list[str]:
        asked.append(pid)
        if isinstance(argv, Exception):
            raise argv
        return argv

    return StartupWarning((LaunchFlagGate(env, read),)).text(), asked


@pytest.mark.parametrize(
    "argv",
    [
        ["claude", "--dangerously-load-development-channels", "server:biff-chan"],
        ["claude", "--dangerously-load-development-channels=plugin:biff@x"],
        ["claude", "--channels", "plugin:biff@punt-labs"],
        ["claude", "--channels=plugin:biff@punt-labs"],
        ["claude", "--channels", "plugin:a@b", "plugin:biff@punt-labs", "-p"],
        ["claude", "--channels", "plugin:biff@punt-labs", "--resume"],
    ],
)
def test_flag_present_is_open(argv: list[str]) -> None:
    text, asked = _warning(ENV, argv)
    assert text is None
    assert asked == [4242]


@pytest.mark.parametrize(
    "argv",
    [
        ["claude"],
        ["claude", "--resume"],
        ["claude", "--channels", "plugin:telegram@official"],
        ["claude", "--channels", "--resume", "plugin:biff@punt-labs"],
    ],
)
def test_flag_missing_warns(argv: list[str]) -> None:
    text, _ = _warning(ENV, argv)
    assert text is not None
    assert "Claude Code was not started with a channel flag for biff" in text
    assert POLL in text
    assert "--dangerously-load-development-channels plugin:biff@punt-labs" in text
    assert "--channels plugin:biff@punt-labs" in text


@pytest.mark.parametrize("env", [{}, {"CLAUDE_PID": ""}, {"CLAUDE_PID": "abc"}])
def test_no_usable_pid_is_unknown_and_silent(
    env: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("INFO"):
        text, asked = _warning(env, ["claude"])
    assert text is None
    assert asked == []
    assert "launch_flag_unknown" in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        psutil.NoSuchProcess(4242),
        psutil.AccessDenied(4242),
        psutil.ZombieProcess(4242),
    ],
)
def test_unreadable_process_is_unknown_and_silent(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("INFO"):
        text, _ = _warning(ENV, error)
    assert text is None
    assert "launch_flag_unknown" in caplog.text


def test_default_reader_reads_a_real_process() -> None:
    env = {"CLAUDE_PID": str(os.getpid())}
    text = StartupWarning((LaunchFlagGate(env),)).text()
    assert text is not None  # the test runner's own argv has no channel flag


def test_for_machine_includes_the_gate() -> None:
    env = {"CLAUDE_PID": str(os.getpid())}
    text = StartupWarning.for_machine(env=env, platform="win32").text()
    assert text is not None
    assert "channel flag" in text
