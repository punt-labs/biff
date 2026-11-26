"""The daemon never resolves a session from its own environment, and says so."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from biff.client.environment import ClientEnvironment, UnsupportedOverrideError
from biff.config import ResolvedConfig
from biff.daemon import resolver as resolver_module
from biff.daemon.environment import DaemonEnvironment
from biff.daemon.resolver import DiskResolver, ResolveError
from biff.frame_client import Hello
from biff.models import BiffConfig

if TYPE_CHECKING:
    from tests.test_daemon.conftest import Harness


def test_scrub_removes_the_identity_and_relay_variables_and_names_them() -> None:
    environ = {
        "BIFF_USER": "x",
        "BIFF_RELAY_URL": "nats://x",
        "BIFF_RELAY_TOKEN": "t",
        "BIFF_RELAY_NKEYS_SEED": "s",
        "BIFF_RELAY_USER_CREDENTIALS": "c",
        "BIFF_RELAY_TLS_HANDSHAKE_FIRST": "1",
        "BIFF_OWNER": "o",
        "BIFF_TELEMETRY": "off",
        "HOME": "/h",
    }
    removed = DaemonEnvironment.scrub(environ)
    assert removed == (
        "BIFF_OWNER",
        "BIFF_RELAY_NKEYS_SEED",
        "BIFF_RELAY_TLS_HANDSHAKE_FIRST",
        "BIFF_RELAY_TOKEN",
        "BIFF_RELAY_URL",
        "BIFF_RELAY_USER_CREDENTIALS",
        "BIFF_USER",
    )
    assert environ == {"BIFF_TELEMETRY": "off", "HOME": "/h"}


def test_scrub_of_a_clean_environment_removes_nothing() -> None:
    assert DaemonEnvironment.scrub({"HOME": "/h"}) == ()


def test_the_clients_user_variable_is_carried_when_no_user_is_given() -> None:
    assert ClientEnvironment.user("", {"BIFF_USER": "eric"}) == "eric"


def test_an_explicit_user_wins_over_the_variable() -> None:
    assert ClientEnvironment.user("kai", {"BIFF_USER": "eric"}) == "kai"


def test_relay_overrides_in_the_clients_environment_are_refused_by_name() -> None:
    environ = {"BIFF_RELAY_URL": "nats://x", "BIFF_RELAY_TOKEN": "t", "HOME": "/h"}
    pattern = "BIFF_RELAY_TOKEN, BIFF_RELAY_URL"
    with pytest.raises(UnsupportedOverrideError, match=pattern):
        ClientEnvironment.require_no_relay_overrides(environ)


def test_a_client_without_relay_overrides_is_accepted() -> None:
    ClientEnvironment.require_no_relay_overrides({"BIFF_USER": "eric"})


async def test_connect_refuses_a_relay_override_before_touching_the_network(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIFF_RELAY_URL", "nats://elsewhere")
    with pytest.raises(UnsupportedOverrideError, match="BIFF_RELAY_URL"):
        await harness.connect("s-kai", "kai")
    assert harness.daemon.status().sessions == ()


async def test_connect_carries_the_clients_user_variable(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIFF_USER", "eric")
    await harness.connect("s-eric", "")
    (session,) = harness.daemon.status().sessions
    assert session.user == "eric"


class _Recorder:
    """Stands in for the two config loaders and records which one ran."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def loader(self, name: str) -> object:
        def load(*, user_override: str | None, start: Path) -> ResolvedConfig:
            self.calls.append(name)
            return ResolvedConfig(
                config=BiffConfig(user=user_override or name, repo_name="r"),
                data_dir=start / "relay",
                repo_common_root=start,
            )

        return load


def _hello(client: str) -> Hello:
    return Hello.model_validate(
        {
            "mac": "ab" * 32,
            "client": client,
            "session_id": "s1",
            "cwd": "/work/repo",
        }
    )


@pytest.mark.parametrize("client", ["mcp", "plugin", "hook"])
async def test_an_agent_side_client_resolves_the_agent_identity(
    client: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hook and an mcp server of one session must name the same user."""
    recorder = _Recorder()
    monkeypatch.setattr(resolver_module, "load_mcp_config", recorder.loader("mcp"))
    monkeypatch.setattr(resolver_module, "load_cli_config", recorder.loader("cli"))
    resolved = await DiskResolver().resolve(_hello(client))
    assert (recorder.calls, resolved.config.user) == (["mcp"], "mcp")


async def test_a_cli_client_resolves_the_human_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(resolver_module, "load_mcp_config", recorder.loader("mcp"))
    monkeypatch.setattr(resolver_module, "load_cli_config", recorder.loader("cli"))
    await DiskResolver().resolve(_hello("cli"))
    assert recorder.calls == ["cli"]


def _failing(exc: BaseException) -> object:
    def load(*, user_override: str | None, start: Path) -> ResolvedConfig:
        del user_override, start
        raise exc

    return load


class _Shape(BaseModel):
    count: int


def _validation_error() -> ValidationError:
    try:
        _Shape.model_validate({"count": "x"})
    except ValidationError as exc:
        return exc
    msg = "validation unexpectedly passed"
    raise AssertionError(msg)


@pytest.mark.parametrize(
    "exc",
    [
        SystemExit(None),
        SystemExit(2),
        SystemExit("bad config"),
        OSError("permission denied"),
        yaml.YAMLError("bad yaml"),
        _validation_error(),
        ValueError("bad value"),
    ],
    ids=lambda e: type(e).__name__ + repr(getattr(e, "code", "")),
)
async def test_every_config_failure_is_a_resolve_error_naming_the_directory(
    exc: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(resolver_module, "load_cli_config", _failing(exc))
    with pytest.raises(ResolveError, match="/work/repo"):
        await DiskResolver().resolve(_hello("cli"))
