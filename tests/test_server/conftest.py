"""Shared fixtures for server tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from biff.models import BiffConfig
from biff.server.state import ServerState, create_state

_TEST_REPO = "_test-server"


@pytest.fixture
def config() -> BiffConfig:
    return BiffConfig(user="kai", repo_name=_TEST_REPO)


@pytest.fixture
def state(tmp_path: Path, config: BiffConfig) -> ServerState:
    return create_state(config, tmp_path, tty="tty1", hostname="test-host", pwd="/test")
