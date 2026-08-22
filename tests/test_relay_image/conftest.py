"""Fixtures for the Docker relay-image tier (``tests/test_relay_image/``).

Builds and runs the real ``ghcr.io/punt-labs/biff-relay`` image from this
checkout's ``docker/`` directory -- see ``_docker.py`` for the mechanics.
Unlike ``tests/test_nats_e2e/``'s bare ``nats-server`` (no Docker, no auth),
this tier exercises ``entrypoint.sh``, the config-file selection, and the
auth-refusal guard PR #376 added.
"""

from __future__ import annotations

import secrets
import uuid
from typing import TYPE_CHECKING

import pytest

from ._docker import DockerRelay, RelayContainer

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(scope="session")
def relay_driver() -> DockerRelay:
    """A ``DockerRelay`` bound to the local ``docker`` binary, or skip."""
    return DockerRelay()


@pytest.fixture(scope="session")
def relay_image(relay_driver: DockerRelay) -> str:
    """Build the image once per test session; return its tag."""
    return relay_driver.build()


@pytest.fixture
def relay_token() -> str:
    """A real random auth token.

    ``entrypoint.sh`` refuses to start with no token at all (see
    ``TestAuthGuard``) -- every other fixture in this tier needs a real one
    to get a running container.
    """
    return secrets.token_hex(32)


@pytest.fixture
def relay_container(
    relay_driver: DockerRelay,
    relay_image: str,
    relay_token: str,
    tmp_path: Path,
) -> Iterator[RelayContainer]:
    """Start a fresh, authenticated relay container for one test.

    Stopped and removed on teardown even if the test raises -- the
    ``finally`` covers both an assertion failure inside the test body and a
    failure during startup (``docker run`` succeeding but the healthcheck
    never turning green).
    """
    name = f"biff-relay-pytest-{uuid.uuid4().hex[:12]}"
    try:
        container = relay_driver.start(
            relay_image, relay_token, tmp_path / "conf", name
        )
        yield container
    finally:
        relay_driver.remove(name)
