"""Shared fixtures for server tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from biff.models import BiffConfig
from biff.server.state import ServerState, create_state


@pytest.fixture
def config() -> BiffConfig:
    return BiffConfig(user="kai")


@pytest.fixture
def state(tmp_path: Path, config: BiffConfig) -> ServerState:
    return create_state(config, tmp_path)
