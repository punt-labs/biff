"""biffd serves the relay settings it was started with; a client may not differ."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from biff.client.connection import ClientIdentity, DaemonClient, DaemonError
from biff.daemon.relays import ResolvedSession
from biff.models import BiffConfig, RelayAuth
from biff.relay_seed import RelayConflictError, RelaySeed
from biff.supervision.health import HealthProbe
from tests.test_daemon.conftest import FakeResolver

if TYPE_CHECKING:
    from biff.frame_client import Hello
    from tests.test_daemon.conftest import Harness

_SEEDED = {"BIFF_RELAY_URL": "nats://relay.invalid:4222", "BIFF_RELAY_TOKEN": "tok-A"}
_SEED_TEXT = "BIFF_RELAY_TOKEN, BIFF_RELAY_URL (relay relay.invalid:4222)"


def test_the_seed_holds_digests_not_values() -> None:
    seed = RelaySeed.of(_SEEDED)
    assert "tok-A" not in repr(seed) + str(seed.to_wire())


def test_the_seed_ignores_unrelated_and_empty_variables() -> None:
    environ = {"HOME": "/h", "BIFF_USER": "kai", "BIFF_RELAY_TOKEN": ""}
    assert not RelaySeed.of(environ)


def test_the_same_values_do_not_conflict() -> None:
    assert RelaySeed.of(_SEEDED).differing_from(RelaySeed.of(_SEEDED)) == ()


def test_a_client_with_no_relay_settings_differs_from_a_seeded_daemon() -> None:
    assert RelaySeed.of({}).differing_from(RelaySeed.of(_SEEDED)) == (
        "BIFF_RELAY_TOKEN",
        "BIFF_RELAY_URL",
    )


def test_a_different_value_conflicts_by_name() -> None:
    other = RelaySeed.of({**_SEEDED, "BIFF_RELAY_TOKEN": "tok-B"})
    assert other.differing_from(RelaySeed.of(_SEEDED)) == ("BIFF_RELAY_TOKEN",)


def test_a_variable_the_daemon_lacks_conflicts() -> None:
    client = RelaySeed.of({"BIFF_RELAY_USER_CREDENTIALS": "/c"})
    assert client.differing_from(RelaySeed.of({})) == ("BIFF_RELAY_USER_CREDENTIALS",)


def test_the_seed_survives_the_wire() -> None:
    seed = RelaySeed.of(_SEEDED)
    assert RelaySeed.from_wire(seed.to_wire()).differing_from(seed) == ()


@pytest.mark.parametrize("client", ["1", "TRUE", " true ", "yes", "Yes"])
def test_every_spelling_of_true_is_the_same_tls_setting(client: str) -> None:
    started = RelaySeed.of({"BIFF_RELAY_TLS_HANDSHAKE_FIRST": "true"})
    spelled = RelaySeed.of({"BIFF_RELAY_TLS_HANDSHAKE_FIRST": client})
    assert spelled.differing_from(started) == ()


def test_true_and_false_differ() -> None:
    started = RelaySeed.of({"BIFF_RELAY_TLS_HANDSHAKE_FIRST": "true"})
    stopped = RelaySeed.of({"BIFF_RELAY_TLS_HANDSHAKE_FIRST": "0"})
    assert stopped.differing_from(started) == ("BIFF_RELAY_TLS_HANDSHAKE_FIRST",)


def test_a_tls_value_the_configuration_ignores_is_not_a_setting() -> None:
    assert not RelaySeed.of({"BIFF_RELAY_TLS_HANDSHAKE_FIRST": "garbage"})


def test_credentials_without_a_url_are_refused_naming_the_variable() -> None:
    with pytest.raises(RelayConflictError, match="BIFF_RELAY_TOKEN") as refused:
        RelaySeed.for_daemon({"BIFF_RELAY_TOKEN": "tok-A"})
    assert "tok-A" not in str(refused.value)


def test_a_url_alone_or_with_one_credential_is_a_seed() -> None:
    assert RelaySeed.for_daemon({"BIFF_RELAY_URL": "nats://x"})
    assert RelaySeed.for_daemon(_SEEDED)


def test_two_credentials_are_refused_naming_both(tmp_path: Path) -> None:
    creds = tmp_path / "c.creds"
    creds.write_text("x")
    environ = {**_SEEDED, "BIFF_RELAY_USER_CREDENTIALS": str(creds)}
    with pytest.raises(
        RelayConflictError, match="BIFF_RELAY_TOKEN, BIFF_RELAY_USER_CREDENTIALS"
    ):
        RelaySeed.for_daemon(environ)


@pytest.mark.parametrize("path", ["relative.creds", "/no/such/file.creds"])
def test_a_credentials_path_must_be_absolute_and_exist(path: str) -> None:
    environ = {
        "BIFF_RELAY_URL": "nats://x",
        "BIFF_RELAY_USER_CREDENTIALS": path,
    }
    with pytest.raises(RelayConflictError, match="BIFF_RELAY_USER_CREDENTIALS"):
        RelaySeed.for_daemon(environ)


def test_a_credentials_file_that_exists_is_a_seed(tmp_path: Path) -> None:
    creds = tmp_path / "c.creds"
    creds.write_text("x")
    environ = {"BIFF_RELAY_URL": "nats://x", "BIFF_RELAY_USER_CREDENTIALS": str(creds)}
    assert RelaySeed.for_daemon(environ)


def test_the_description_names_variables_and_the_host_never_a_value() -> None:
    seed = RelaySeed.of(
        {"BIFF_RELAY_URL": "tls://user:pw@relay.example:4222", "BIFF_RELAY_TOKEN": "t"}
    )
    assert seed.describe() == (
        "BIFF_RELAY_TOKEN, BIFF_RELAY_URL (relay relay.example:4222)"
    )


def test_seeded_credentials_stay_with_the_seeded_url() -> None:
    RelaySeed.of(_SEEDED).require_credentials_home(
        "nats://relay.invalid:4222", RelayAuth(token="tok-A")
    )


def test_a_url_is_compared_in_its_normal_form() -> None:
    RelaySeed.of(_SEEDED).require_credentials_home(
        "NATS://Relay.invalid:4222/", RelayAuth(token="tok-A")
    )


def test_seeded_credentials_do_not_follow_a_session_to_another_relay() -> None:
    with pytest.raises(RelayConflictError, match="BIFF_RELAY_URL") as refused:
        RelaySeed.of(_SEEDED).require_credentials_home(
            "nats://elsewhere:4222", RelayAuth(token="tok-A")
        )
    assert "tok-A" not in str(refused.value)


def test_the_sessions_own_credentials_may_go_to_its_own_relay() -> None:
    RelaySeed.of(_SEEDED).require_credentials_home(
        "nats://elsewhere:4222", RelayAuth(token="its-own")
    )


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


async def test_a_client_with_no_relay_environment_is_refused_naming_the_seed(
    harness: Harness,
) -> None:
    with pytest.raises(DaemonError, match="started with BIFF_RELAY_TOKEN") as refused:
        await harness.connect("s-kai", "kai")
    assert "tok-A" not in str(refused.value)
    assert harness.daemon.status().sessions == ()


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
    for name, value in _SEEDED.items():
        monkeypatch.setenv(name, value)
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


async def test_a_session_resolved_to_another_relay_with_the_seeded_token_is_refused(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def resolve(self: FakeResolver, hello: Hello) -> ResolvedSession:
        return ResolvedSession(
            config=BiffConfig(
                user="kai",
                repo_name="r",
                relay_url="nats://elsewhere:4222",
                relay_auth=RelayAuth(token="tok-A"),
            ),
            data_dir=Path("/unused"),
            repo_root=None,
        )

    monkeypatch.setattr(FakeResolver, "resolve", resolve)
    with pytest.raises(DaemonError, match="BIFF_RELAY_URL") as refused:
        await harness.connect("s-kai", "kai")
    assert "tok-A" not in str(refused.value)


async def test_the_hello_reply_tells_the_client_what_biffd_was_seeded_with(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in _SEEDED.items():
        monkeypatch.setenv(name, value)
    kai = await harness.connect("s-kai", "kai")
    assert kai.relay_seed == _SEED_TEXT


async def test_health_reports_the_seed_without_a_value(harness: Harness) -> None:
    probe = HealthProbe(harness.daemon.port)
    health = await asyncio.to_thread(probe.fetch, 5.0)
    assert health.relay_seed == _SEED_TEXT
