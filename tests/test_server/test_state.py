"""Tests for server state container."""

from __future__ import annotations

from pathlib import Path

import pytest

from biff.models import BiffConfig, RelayAuth
from biff.nats_relay import NatsRelay
from biff.relay import LocalRelay
from biff.server.state import create_state

_TEST_REPO = "_test-server"


class TestCreateState:
    def test_creates_relay(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai", repo_name=_TEST_REPO)
        state = create_state(config, tmp_path)
        assert isinstance(state.relay, LocalRelay)
        assert state.config is config

    def test_selects_nats_relay_when_relay_url_set(self, tmp_path: Path) -> None:
        config = BiffConfig(
            user="kai", repo_name=_TEST_REPO, relay_url="nats://localhost:4222"
        )
        state = create_state(config, tmp_path)
        assert isinstance(state.relay, NatsRelay)

    def test_selects_local_relay_when_no_relay_url(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai", repo_name=_TEST_REPO)
        state = create_state(config, tmp_path)
        assert isinstance(state.relay, LocalRelay)

    def test_empty_relay_url_falls_back_to_local(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai", repo_name=_TEST_REPO, relay_url="")
        state = create_state(config, tmp_path)
        assert isinstance(state.relay, LocalRelay)

    def test_explicit_relay_overrides_config(self, tmp_path: Path) -> None:
        config = BiffConfig(
            user="kai", repo_name=_TEST_REPO, relay_url="nats://localhost:4222"
        )
        local = LocalRelay(data_dir=tmp_path)
        state = create_state(config, tmp_path, relay=local)
        assert isinstance(state.relay, LocalRelay)

    def test_relay_auth_forwarded_to_nats_relay(self, tmp_path: Path) -> None:
        auth = RelayAuth(user_credentials="/path/to.creds")
        config = BiffConfig(
            user="kai", repo_name=_TEST_REPO, relay_url="tls://host", relay_auth=auth
        )
        state = create_state(config, tmp_path)
        assert isinstance(state.relay, NatsRelay)
        assert state.relay._auth is auth

    def test_no_relay_auth_when_none(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai", repo_name=_TEST_REPO, relay_url="nats://host")
        state = create_state(config, tmp_path)
        assert isinstance(state.relay, NatsRelay)
        assert state.relay._auth is None

    def test_state_is_frozen(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai", repo_name=_TEST_REPO)
        state = create_state(config, tmp_path)
        with pytest.raises(AttributeError):
            state.config = BiffConfig(user="other", repo_name=_TEST_REPO)  # type: ignore[misc]


class TestNatsRelayAuthKwargs:
    """Unit tests for NatsRelay._auth_kwargs() — no NATS server needed."""

    def test_no_auth(self) -> None:
        relay = NatsRelay(url="nats://host")
        assert relay._auth_kwargs() == {}

    def test_token(self) -> None:
        relay = NatsRelay(url="nats://host", auth=RelayAuth(token="s3cret"))
        assert relay._auth_kwargs() == {"token": "s3cret"}

    def test_nkeys_seed(self) -> None:
        auth = RelayAuth(nkeys_seed="/path/to.nk")
        relay = NatsRelay(url="nats://host", auth=auth)
        assert relay._auth_kwargs() == {"nkeys_seed": "/path/to.nk"}

    def test_user_credentials(self) -> None:
        auth = RelayAuth(user_credentials="/path/to.creds")
        relay = NatsRelay(url="nats://host", auth=auth)
        assert relay._auth_kwargs() == {"user_credentials": "/path/to.creds"}
