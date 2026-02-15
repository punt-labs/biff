"""Tests for server state container."""

from __future__ import annotations

from pathlib import Path

import pytest

from biff.models import BiffConfig
from biff.nats_relay import NatsRelay
from biff.relay import LocalRelay
from biff.server.state import create_state


class TestCreateState:
    def test_creates_relay(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai")
        state = create_state(config, tmp_path)
        assert isinstance(state.relay, LocalRelay)
        assert state.config is config

    def test_selects_nats_relay_when_relay_url_set(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai", relay_url="nats://localhost:4222")
        state = create_state(config, tmp_path)
        assert isinstance(state.relay, NatsRelay)

    def test_selects_local_relay_when_no_relay_url(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai")
        state = create_state(config, tmp_path)
        assert isinstance(state.relay, LocalRelay)

    def test_explicit_relay_overrides_config(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai", relay_url="nats://localhost:4222")
        local = LocalRelay(data_dir=tmp_path)
        state = create_state(config, tmp_path, relay=local)
        assert isinstance(state.relay, LocalRelay)

    def test_state_is_frozen(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai")
        state = create_state(config, tmp_path)
        with pytest.raises(AttributeError):
            state.config = BiffConfig(user="other")  # type: ignore[misc]
