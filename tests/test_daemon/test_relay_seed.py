"""biffd serves the relay settings it was started with; a client may not differ."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from biff.client.connection import ClientIdentity, DaemonClient, DaemonError
from biff.relay_seed import RelaySeed

if TYPE_CHECKING:
    from tests.test_daemon.conftest import Harness

_SEEDED = {"BIFF_RELAY_URL": "nats://relay.invalid:4222", "BIFF_RELAY_TOKEN": "tok-A"}


def test_the_seed_holds_digests_not_values() -> None:
    seed = RelaySeed.of(_SEEDED)
    assert "tok-A" not in repr(seed) + str(seed.to_wire())


def test_the_seed_ignores_unrelated_and_empty_variables() -> None:
    environ = {"HOME": "/h", "BIFF_USER": "kai", "BIFF_RELAY_TOKEN": ""}
    assert not RelaySeed.of(environ)


def test_the_same_values_do_not_conflict() -> None:
    assert RelaySeed.of(_SEEDED).conflicts_with(RelaySeed.of(_SEEDED)) == ()


def test_a_client_with_no_relay_settings_does_not_conflict() -> None:
    assert RelaySeed.of({}).conflicts_with(RelaySeed.of(_SEEDED)) == ()


def test_a_different_value_conflicts_by_name() -> None:
    other = RelaySeed.of({**_SEEDED, "BIFF_RELAY_TOKEN": "tok-B"})
    assert other.conflicts_with(RelaySeed.of(_SEEDED)) == ("BIFF_RELAY_TOKEN",)


def test_a_variable_the_daemon_lacks_conflicts() -> None:
    client = RelaySeed.of({"BIFF_RELAY_USER_CREDENTIALS": "/c"})
    assert client.conflicts_with(RelaySeed.of(_SEEDED)) == (
        "BIFF_RELAY_USER_CREDENTIALS",
    )


def test_the_seed_survives_the_wire() -> None:
    seed = RelaySeed.of(_SEEDED)
    assert RelaySeed.from_wire(seed.to_wire()).conflicts_with(seed) == ()


@pytest.fixture
def relay_seed() -> RelaySeed:
    return RelaySeed.of(_SEEDED)


async def test_a_client_with_the_same_environment_is_accepted(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in _SEEDED.items():
        monkeypatch.setenv(name, value)
    kai = await harness.connect("s-kai", "kai")
    assert await kai.request("plan", message="same relay") is not None


async def test_a_client_with_no_relay_environment_is_accepted(
    harness: Harness,
) -> None:
    kai = await harness.connect("s-kai", "kai")
    assert await kai.request("plan", message="no env") is not None


async def test_a_client_with_another_token_is_refused_naming_the_variable(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIFF_RELAY_URL", _SEEDED["BIFF_RELAY_URL"])
    monkeypatch.setenv("BIFF_RELAY_TOKEN", "tok-B")
    with pytest.raises(DaemonError, match="BIFF_RELAY_TOKEN") as refused:
        await harness.connect("s-eric", "eric")
    assert "tok-A" not in str(refused.value)
    assert "tok-B" not in str(refused.value)


async def test_a_refused_client_leaves_no_session(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIFF_RELAY_TOKEN", "tok-B")
    with pytest.raises(DaemonError):
        await harness.connect("s-eric", "eric")
    assert harness.daemon.status().sessions == ()


async def test_a_client_that_only_ends_a_session_is_not_compared(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An end attaches to the session biffd already holds; it resolves no relay."""
    await harness.connect("s-kai", "kai", client="mcp")
    monkeypatch.setenv("BIFF_RELAY_TOKEN", "tok-B")
    ender = await DaemonClient.connect(
        run_dir=harness.run_dir,
        port=harness.daemon.port,
        identity=ClientIdentity(
            client="hook",
            session_id="s-kai",
            cwd="/work/repo",
            main_agent_id="m1",
            user="kai",
            ends=True,
        ),
    )
    await ender.close()
