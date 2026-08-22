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
    """A running biff-relay container ready for connections.

    ``token`` is ``None`` for a container started via ``start_default()`` --
    the no-mounted-config path selects ``base.conf``, which has no
    ``authorization`` block at all.
    """

    url: str
    token: str | None
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

    def bridge_ip(self, name: str) -> str:
        """Return ``name``'s IP address on Docker's default bridge network.

        Used to reach the container from a sibling container over the
        Docker network -- distinct from, and more portable than, reaching
        it via a host-published port. Host-side port publishing goes
        through per-platform proxy/NAT machinery (e.g. Docker Desktop's
        vpnkit on macOS forwards published ports straight into a
        container's loopback interface, unlike Linux's iptables DNAT,
        which cannot reach a loopback-only bind) -- so it can't reliably
        prove a loopback-only bind refuses outside connections. A sibling
        container reaching this container's own bridge IP behaves the same
        genuine Linux bridge networking on every host.
        """
        result = subprocess.run(  # noqa: S603
            [
                self._docker,
                "inspect",
                "--format",
                "{{.NetworkSettings.Networks.bridge.IPAddress}}",
                name,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip()

    def probe_http_reachable(self, image: str, host: str, port: int) -> bool:
        """Return whether ``http://<host>:<port>/healthz`` answers.

        Runs ``image``'s own BusyBox ``wget`` (``--entrypoint`` overrides
        ``/entrypoint.sh``) in a one-shot sibling container attached to the
        default bridge network, so no extra probe image needs pulling.
        """
        result = subprocess.run(  # noqa: S603
            [
                self._docker,
                "run",
                "--rm",
                "--entrypoint",
                "wget",
                image,
                "-q",
                "-T",
                "2",
                "--spider",
                f"http://{host}:{port}/healthz",
            ],
            capture_output=True,
            timeout=_DOCKER_TIMEOUT_S,
            check=False,
        )
        return result.returncode == 0

    def start_default(self, image: str, name: str) -> RelayContainer:
        """Start ``image`` with no mounted ``nats.conf`` -- the default path.

        Matches the individual-tier ``docker run`` example in
        ``docs/self-hosted-relay.md``: no ``-v`` mount at all, so
        ``entrypoint.sh`` selects ``base.conf`` instead of
        ``base-with-user.conf``, and no authentication is configured or
        required -- that is this tier's documented trust model.
        """
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
                image,
            ],
            check=True,
            capture_output=True,
            timeout=_DOCKER_TIMEOUT_S,
        )
        self.wait_healthy(name)
        return RelayContainer(url=f"nats://127.0.0.1:{port}", token=None, name=name)

    def run_to_exit(
        self, image: str, conf_dir: Path, conf_text: str, name: str
    ) -> subprocess.CompletedProcess[str]:
        """Run ``image`` synchronously (no ``-d``) with ``conf_text`` mounted.

        For asserting ``entrypoint.sh``'s exit-1 auth-refusal path: no
        healthcheck wait, no background container, just the process's own
        exit code and stderr. ``--rm`` cleans up the container on exit --
        but only on exit. If the auth-refusal guard regresses and the
        container keeps running instead of exiting, ``subprocess.run``'s
        ``timeout`` fires and ``--rm`` never gets to run, leaking a live
        container; force-remove it before re-raising so a regression here
        doesn't also leak infrastructure.
        """
        conf_dir.mkdir(parents=True, exist_ok=True)
        conf_path = conf_dir / "nats.conf"
        conf_path.write_text(conf_text)
        try:
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
        except subprocess.TimeoutExpired:
            self.remove(name)
            raise

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
        """Stop and remove ``name`` and its anonymous volumes.

        ``-v`` is required: plain ``docker rm -f`` leaves the Dockerfile's
        anonymous ``VOLUME /data`` behind, so every test run without it
        would leak one volume per container. Never raises -- always safe
        in teardown.
        """
        subprocess.run(  # noqa: S603
            [self._docker, "rm", "-fv", name],
            check=False,
            capture_output=True,
            timeout=_DOCKER_TIMEOUT_S,
        )
