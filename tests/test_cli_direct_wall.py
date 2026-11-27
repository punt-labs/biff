"""``biff wall`` posts straight to the relay on a CI runner, and only then."""

from __future__ import annotations

import asyncio
import subprocess
from typing import TYPE_CHECKING

import pytest
from nats.errors import Error as NatsError
from typer.testing import CliRunner

from biff import cli_daemon, cli_direct_wall
from biff.__main__ import app
from biff.config import ResolvedConfig
from biff.models import BiffConfig
from biff.nats_relay import NatsRelay
from biff.relay import LocalRelay
from biff.relay_select import RelaySelector

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from biff.models import WallPost
    from biff.relay import Relay

_RELAY_SETTING = {"BIFF_RELAY_URL": "nats://relay.invalid:4222"}


class _NoDaemon:
    """A nudge that fails the test if anything tries to start biffd."""

    @classmethod
    def for_host(cls, run_dir: object) -> _NoDaemon:
        raise AssertionError("a direct wall must not start or reach biffd")

    def ensure_running(self) -> None:
        raise AssertionError("a direct wall must not start or reach biffd")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git repository under a fresh HOME, as on a CI runner."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    path = tmp_path / "work" / "demo"
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)  # noqa: S603, S607
    monkeypatch.chdir(path)
    return path


def test_a_wall_with_relay_settings_in_the_environment_goes_straight_to_the_relay(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_daemon, "DaemonNudge", _NoDaemon)
    shared = LocalRelay(tmp_path / "relay")  # stands in for the NATS relay

    def select(self: RelaySelector, *, dormant: bool) -> LocalRelay:
        return shared

    monkeypatch.setattr(RelaySelector, "select", select)
    result = CliRunner().invoke(
        app,
        ["--user", "ci", "wall", "build failed", "--duration", "1h"],
        env=_RELAY_SETTING,
    )
    assert result.exit_code == 0, result.output
    assert relay_wall(shared) == "build failed"
    assert not (tmp_path / "home" / ".punt-labs" / "biff" / "run").exists()


def relay_wall(relay: LocalRelay) -> str:
    """Return the text of the wall the relay holds."""
    current = asyncio.run(relay.get_wall())
    assert current is not None
    return current.text


class _RecordingNats(NatsRelay):
    """A NATS relay whose KV write is observable and can be made to fail."""

    def __init__(self, discovered: frozenset[str], failure: Exception | None) -> None:
        super().__init__(url="nats://relay.invalid:4222", repo_name="demo")
        self._discovered = discovered
        self._failure = failure
        self.written: list[str] = []

    async def discover_repos_for_orgs(self, orgs: Iterable[str]) -> frozenset[str]:
        return self._discovered

    async def set_wall_for_repo(self, repo: str, wall: WallPost | None) -> None:
        if self._failure is not None:
            raise self._failure
        self.written.append(repo)

    async def close(self) -> None:
        return None


def _use_relay(monkeypatch: pytest.MonkeyPatch, relay: Relay, tmp_path: Path) -> None:
    config = BiffConfig(
        user="ci",
        repo_name="demo",
        relay_url="nats://relay.invalid:4222",
        orgs=("acme",),
    )
    resolved = ResolvedConfig(
        config=config, data_dir=tmp_path, repo_common_root=tmp_path
    )

    def load(user_override: str | None = None) -> ResolvedConfig:
        return resolved

    def select(self: RelaySelector, *, dormant: bool) -> Relay:
        return relay

    monkeypatch.setattr(cli_direct_wall, "load_cli_config", load)
    monkeypatch.setattr(RelaySelector, "select", select)


def test_a_ci_wall_reaches_every_repository_of_the_organization(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relay = _RecordingNats(frozenset({"acme-api", "acme-web"}), None)
    _use_relay(monkeypatch, relay, tmp_path)
    result = CliRunner().invoke(
        app, ["--user", "ci", "wall", "build failed"], env=_RELAY_SETTING
    )
    assert result.exit_code == 0, result.output
    assert set(relay.written) == {"demo", "acme-api", "acme-web"}


def test_a_ci_wall_the_relay_rejects_exits_non_zero_with_the_relays_error(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relay = _RecordingNats(frozenset(), NatsError("authorization violation"))
    _use_relay(monkeypatch, relay, tmp_path)
    result = CliRunner().invoke(
        app, ["--user", "ci", "wall", "build failed"], env=_RELAY_SETTING
    )
    assert (result.exit_code, "authorization violation" in result.output) == (
        1,
        True,
    )
