"""The autostart nudge in a real process on a host with no service manager.

A bogus ``XDG_RUNTIME_DIR`` makes ``systemctl --user`` fail to reach a bus, which
is what an agent-image container looks like to biff.  The child runs with a
throwaway ``HOME`` so its token, port file and log stay out of the developer's.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from biff.run_dir import RunDir
from biff.supervision.health import HealthProbe
from biff.supervision.runner import Supervisor

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.subprocess

_NUDGE: Final = textwrap.dedent("""\
    import dataclasses
    import sys
    from biff.client.nudge import DaemonNudge
    from biff.run_dir import RunDir
    from biff.supervision.biffd import BIFFD_SPEC

    spec = dataclasses.replace(
        BIFFD_SPEC, health_port=int(sys.argv[1]), beside_interpreter=False
    )
    try:
        DaemonNudge.for_host(RunDir.default(), spec).ensure_running()
    except ConnectionError as exc:
        print(exc)
        sys.exit(1)
    print("answers")
""")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run(home: Path, port: int) -> subprocess.CompletedProcess[str]:
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "XDG_RUNTIME_DIR": str(home / "no-bus"),
        "PUNT_TELEMETRY": "off",
    }
    return subprocess.run(
        [sys.executable, "-c", _NUDGE, str(port)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / ".local" / "bin").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def port(home: Path) -> Iterator[int]:
    """A free port recorded as the daemon's; any biffd left on it is killed."""
    number = _free_port()
    # A crash left a record of a port nothing listens on; the unit port wins.
    RunDir(home / ".punt-labs" / "biff" / "run").write_port(1)
    yield number
    Supervisor.run("pkill", "-f", f"biffd --port {number}")


def test_without_the_executable_the_nudge_says_to_install(
    home: Path, port: int
) -> None:
    result = _run(home, port)
    assert (result.returncode, result.stdout.strip()) == (
        1,
        "biffd is not installed. Run: biff install",
    ), result.stderr


def test_with_the_executable_the_nudge_starts_biffd_detached(
    home: Path, port: int
) -> None:
    (home / ".local" / "bin" / "biffd").symlink_to(
        Path(sys.executable).parent / "biffd"
    )
    result = _run(home, port)
    assert (result.returncode, result.stdout.strip()) == (0, "answers"), result.stderr
    assert HealthProbe(port).answers(1.0)
