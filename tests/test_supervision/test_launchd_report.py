"""What ``launchctl print`` says about biffd, read from the job's own block."""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.supervision.biffd import BIFFD_SPEC
from biff.supervision.launchd import LaunchdBackend

if TYPE_CHECKING:
    from tests.test_supervision.scripted import ScriptedSupervisor

# The structure of a real ``launchctl print gui/<uid>/<label>`` of a running job:
# the job's own keys are one tab deep; ``endpoints`` and ``coalition`` are nested
# blocks that repeat ``state`` and ``active count`` deeper down.
RUNNING_JOB = "\n".join(
    [
        "gui/501/com.punt-labs.biffd = {",
        "\tactive count = 1",
        "\tpath = /Users/me/Library/LaunchAgents/com.punt-labs.biffd.plist",
        "\ttype = LaunchAgent",
        "\tstate = running",
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
        "\truns = 1",
        "\tpid = 70860",
        "\timmediate reason = speculative",
        "\tlast exit code = (never exited)",
        "",
        "\tendpoints = {",
        '\t\t"com.punt-labs.biffd" = {',
        "\t\t\tport = 0x1b03",
        "\t\t\tactive = 0",
        "\t\t\tstate = active",
        "\t\t\tactive count = 1",
        "\t\t}",
        "\t}",
        "",
        "\tcoalition = {",
        "\t\tID = 4521",
        "\t\tstate = active",
        "\t\tactive count = 1",
        "\t}",
        "}",
    ]
)


def test_the_report_has_each_top_level_key_once(
    script: ScriptedSupervisor,
) -> None:
    script.answer("launchctl", out=RUNNING_JOB)
    report = LaunchdBackend(BIFFD_SPEC).diagnosis().splitlines()
    assert report == [
        "active count = 1",
        "state = running",
        "runs = 1",
        "pid = 70860",
        "immediate reason = speculative",
        "last exit code = (never exited)",
    ]


def test_a_nested_block_does_not_repeat_the_state(
    script: ScriptedSupervisor,
) -> None:
    script.answer("launchctl", out=RUNNING_JOB)
    report = LaunchdBackend(BIFFD_SPEC).diagnosis()
    assert (report.count("state ="), report.count("active count =")) == (1, 1)


def test_a_job_launchd_has_not_loaded_is_not_loaded(
    script: ScriptedSupervisor,
) -> None:
    script.answer("launchctl", rc=113, err="Could not find service")
    assert LaunchdBackend(BIFFD_SPEC).is_loaded() is False


def test_a_job_launchd_knows_is_loaded_even_when_it_is_not_running(
    script: ScriptedSupervisor,
) -> None:
    script.answer(
        "launchctl", out=RUNNING_JOB.replace("state = running", "state = waiting")
    )
    assert LaunchdBackend(BIFFD_SPEC).is_loaded() is True
