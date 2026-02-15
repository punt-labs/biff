"""Tests for server state container."""

from __future__ import annotations

from pathlib import Path

import pytest

from biff.models import BiffConfig
from biff.relay import LocalRelay
from biff.server.state import create_state


class TestCreateState:
    def test_creates_relay(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai")
        state = create_state(config, tmp_path)
        assert isinstance(state.relay, LocalRelay)
        assert state.config is config

    def test_state_is_frozen(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai")
        state = create_state(config, tmp_path)
        with pytest.raises(AttributeError):
            state.config = BiffConfig(user="other")  # type: ignore[misc]
