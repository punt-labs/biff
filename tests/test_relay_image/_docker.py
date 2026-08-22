"""Docker orchestration for the biff-relay image test tier.

Wraps ``docker build``/``run``/``stop``/``start``/``rm`` via subprocess --
mirrors ``tests/conftest.py``'s ``nats_server`` fixture (skip when the
binary is missing, poll-for-ready loop, always-teardown) but drives a real
container instead of a bare ``nats-server`` process, so callers exercise
``entrypoint.sh``'s config selection and the loopback monitor bind instead
of bypassing them.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Self

import pytest

_DOCKER_DIR: Final[Path] = Path(__file__).resolve().parent.parent.parent / "docker"
_IMAGE_TAG: Final[str] = "biff-relay-pytest:local"
_HEALTHY_TIMEOUT_S: Final[float] = 20.0
_DOCKER_TIMEOUT_S: Final[float] = 30.0
_BUILD_TIMEOUT_S: Final[float] = 180.0


@dataclass(frozen=True, slots=True)
class RelayContainer:
    """A running, authenticated biff-relay container ready for connections."""

    url: str
    token: str
    name: str


def docker_exe() -> str:
    """Return the ``docker`` binary on ``PATH``, or skip the test.

    Mirrors ``tests/conftest.py``'s ``nats_server`` fixture: this tier
    needs a local binary (``docker``, there ``nats-server``) and skips
    rather than fails when it is absent -- the same tradeoff tier 3b makes.
    """
    exe = shutil.which("docker")
    if exe is None:
        pytest.skip("docker not found on PATH")
    return exe


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


class DockerRelay:
    """Builds and runs ``ghcr.io/punt-labs/biff-relay`` from this checkout.

    Built fresh from ``docker/`` on every test session (never pulled from a
    published tag), so this tier exercises whatever a PR actually changed
    in ``docker/``, not what shipped in the last release.
    """

    __slots__ = ("_docker",)

    _docker: str

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._docker = docker_exe()
        return self

    def build(self) -> str:
        """Build the image and return its tag."""
        subprocess.run(  # noqa: S603
            [self._docker, "build", "-t", _IMAGE_TAG, str(_DOCKER_DIR)],
            check=True,
            capture_output=True,
            timeout=_BUILD_TIMEOUT_S,
        )
        return _IMAGE_TAG

    def start(
        self, image: str, token: str, conf_dir: Path, name: str
    ) -> RelayContainer:
        """Start ``image`` with a real token-bearing ``nats.conf`` mounted.

        ``entrypoint.sh``'s auth guard (PR #376) refuses to start with no
        mounted config, an authless one, or the literal placeholder token --
        a real random *token* is required to get a running container at
        all. Waits for the container's own ``HEALTHCHECK`` to report
        healthy before returning.
        """
        conf_dir.mkdir(parents=True, exist_ok=True)
        conf_path = conf_dir / "nats.conf"
        conf_path.write_text(f'authorization {{\n  token: "{token}"\n}}\n')
        port = _find_free_port()
        subprocess.run(  # noqa: S603
            [
                self._docker,
                "run",
                "-d",
                "--name",
                name,
                "-p",
                f"127.0.0.1:{port}:4222",
                "-v",
                f"{conf_path}:/etc/nats/nats.conf:ro",
                image,
            ],
            check=True,
            capture_output=True,
            timeout=_DOCKER_TIMEOUT_S,
        )
        self.wait_healthy(name)
        return RelayContainer(url=f"nats://127.0.0.1:{port}", token=token, name=name)

    def run_to_exit(
        self, image: str, conf_dir: Path, conf_text: str, name: str
    ) -> subprocess.CompletedProcess[str]:
        """Run ``image`` synchronously (no ``-d``) with ``conf_text`` mounted.

        For asserting ``entrypoint.sh``'s exit-1 auth-refusal path: no
        healthcheck wait, no background container, just the process's own
        exit code and stderr. ``--rm`` cleans up the container on exit
        either way.
        """
        conf_dir.mkdir(parents=True, exist_ok=True)
        conf_path = conf_dir / "nats.conf"
        conf_path.write_text(conf_text)
        return subprocess.run(  # noqa: S603
            [
                self._docker,
                "run",
                "--rm",
                "--name",
                name,
                "-v",
                f"{conf_path}:/etc/nats/nats.conf:ro",
                image,
            ],
            capture_output=True,
            text=True,
            timeout=_DOCKER_TIMEOUT_S,
            check=False,
        )

    def wait_healthy(self, name: str, timeout: float = _HEALTHY_TIMEOUT_S) -> None:
        """Poll ``docker inspect``'s ``Health.Status`` until healthy or timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = subprocess.run(  # noqa: S603
                [
                    self._docker,
                    "inspect",
                    "--format",
                    "{{.State.Health.Status}}",
                    name,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if result.stdout.strip() == "healthy":
                return
            time.sleep(0.5)
        logs = subprocess.run(  # noqa: S603
            [self._docker, "logs", name],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        pytest.fail(
            f"{name} did not become healthy within {timeout}s\n"
            f"stdout:\n{logs.stdout}\nstderr:\n{logs.stderr}"
        )

    def stop(self, name: str) -> None:
        subprocess.run(  # noqa: S603
            [self._docker, "stop", name],
            check=True,
            capture_output=True,
            timeout=_DOCKER_TIMEOUT_S,
        )

    def restart(self, name: str) -> None:
        """Start a stopped container back up and wait for it to be healthy."""
        subprocess.run(  # noqa: S603
            [self._docker, "start", name],
            check=True,
            capture_output=True,
            timeout=_DOCKER_TIMEOUT_S,
        )
        self.wait_healthy(name)

    def remove(self, name: str) -> None:
        """Stop and remove ``name``, never raising -- always safe in teardown."""
        subprocess.run(  # noqa: S603
            [self._docker, "rm", "-f", name],
            check=False,
            capture_output=True,
            timeout=_DOCKER_TIMEOUT_S,
        )
