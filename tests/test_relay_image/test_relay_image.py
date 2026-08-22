"""End-to-end tests against the real ghcr.io/punt-labs/biff-relay image.

Exercises what tests/test_nats_e2e/ (bare nats-server, no Docker, no auth)
and tests/test_hosted_nats/ (Synadia Cloud) cannot reach: entrypoint.sh's
config-file selection, the loopback monitor bind, the auth-refusal guard
PR #376 added, and JetStream persistence across a real container restart.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport

from biff.models import BiffConfig, Message, RelayAuth
from biff.nats_relay import NatsRelay
from biff.server.app import create_server
from biff.server.state import create_state
from biff.testing import RecordingClient, Transcript

if TYPE_CHECKING:
    from pathlib import Path

    from ._docker import DockerRelay, RelayContainer

pytestmark = pytest.mark.nats_docker

_TEST_REPO = "_test-relay-image"
_KAI_TTY = "aaaa0011"
_ERIC_TTY = "eeee0022"


def _client_config(container: RelayContainer, user: str) -> BiffConfig:
    return BiffConfig(
        user=user,
        repo_name=_TEST_REPO,
        relay_url=container.url,
        relay_auth=RelayAuth(token=container.token),
    )


class TestPresenceAndMessaging:
    """/who and write/read against the real Docker image, not a mock."""

    async def test_who_round_trip(
        self, relay_container: RelayContainer, tmp_path: Path
    ) -> None:
        """kai sets a plan; eric sees it via /who -- through the real image."""
        kai_state = create_state(
            _client_config(relay_container, "kai"),
            tmp_path / "kai",
            tty=_KAI_TTY,
            hostname="test-host",
            pwd="/test",
        )
        eric_state = create_state(
            _client_config(relay_container, "eric"),
            tmp_path / "eric",
            tty=_ERIC_TTY,
            hostname="test-host",
            pwd="/test",
        )
        transcript = Transcript(title="")
        async with (
            Client(FastMCPTransport(create_server(kai_state))) as kai_raw,
            Client(FastMCPTransport(create_server(eric_state))) as eric_raw,
        ):
            kai = RecordingClient(client=kai_raw, transcript=transcript, user="kai")
            eric = RecordingClient(client=eric_raw, transcript=transcript, user="eric")

            await kai.call("plan", message="verifying the relay image")
            result = await eric.call("who")

        assert "kai" in result

    async def test_message_write_read_pop_semantics(
        self, relay_container: RelayContainer, tmp_path: Path
    ) -> None:
        """A written message is delivered once -- WORK_QUEUE POP, not PubSub."""
        kai_state = create_state(
            _client_config(relay_container, "kai"),
            tmp_path / "kai",
            tty=_KAI_TTY,
            hostname="test-host",
            pwd="/test",
        )
        eric_state = create_state(
            _client_config(relay_container, "eric"),
            tmp_path / "eric",
            tty=_ERIC_TTY,
            hostname="test-host",
            pwd="/test",
        )
        transcript = Transcript(title="")
        async with (
            Client(FastMCPTransport(create_server(kai_state))) as kai_raw,
            Client(FastMCPTransport(create_server(eric_state))) as eric_raw,
        ):
            kai = RecordingClient(client=kai_raw, transcript=transcript, user="kai")
            eric = RecordingClient(client=eric_raw, transcript=transcript, user="eric")

            await kai.call("write", to="@eric", message="relay image works")
            first_read = await eric.call("read_messages")
            second_read = await eric.call("read_messages")

        assert "relay image works" in first_read
        assert "No new messages" in second_read


class TestJetStreamPersistence:
    """Messages written before a stop/start cycle survive it."""

    async def test_message_survives_container_restart(
        self,
        relay_driver: DockerRelay,
        relay_container: RelayContainer,
    ) -> None:
        """JetStream's file store outlives a docker stop/start cycle.

        No explicit ``-v`` volume flag is passed anywhere in this tier --
        Docker allocates an anonymous volume for the Dockerfile's declared
        ``VOLUME /data``, which a stop/start (not ``rm``) cycle reattaches
        to automatically. See DES-059's Persistence table.
        """
        auth = RelayAuth(token=relay_container.token)
        relay = NatsRelay(url=relay_container.url, auth=auth, repo_name=_TEST_REPO)
        try:
            await relay.deliver(
                Message(from_user="kai", to_user="eric:tty1", body="still here")
            )
        finally:
            await relay.close()

        relay_driver.stop(relay_container.name)
        relay_driver.restart(relay_container.name)

        relay2 = NatsRelay(url=relay_container.url, auth=auth, repo_name=_TEST_REPO)
        try:
            messages = await relay2.fetch("eric:tty1")
            assert [m.body for m in messages] == ["still here"]
        finally:
            await relay2.delete_infrastructure()
            await relay2.close()


class TestAuthGuard:
    """entrypoint.sh's exit-1 refusal (PR #376), not just Dockerfile parsing."""

    def test_refuses_start_without_auth_block(
        self, relay_driver: DockerRelay, relay_image: str, tmp_path: Path
    ) -> None:
        """A mounted nats.conf with no authorization/accounts/nkeys is refused."""
        name = f"biff-relay-pytest-noauth-{uuid.uuid4().hex[:12]}"
        result = relay_driver.run_to_exit(
            relay_image,
            tmp_path / "noauth",
            "# no authorization, accounts, or nkeys block\n",
            name,
        )
        assert result.returncode == 1, (result.stdout, result.stderr)
        assert "refusing to start" in result.stderr

    def test_refuses_start_with_placeholder_token(
        self, relay_driver: DockerRelay, relay_image: str, tmp_path: Path
    ) -> None:
        """The unedited example token from nats.conf.team.example is refused."""
        name = f"biff-relay-pytest-placeholder-{uuid.uuid4().hex[:12]}"
        result = relay_driver.run_to_exit(
            relay_image,
            tmp_path / "placeholder",
            'authorization {\n  token: "REPLACE_WITH_A_LONG_RANDOM_TOKEN"\n}\n',
            name,
        )
        assert result.returncode == 1, (result.stdout, result.stderr)
        assert "placeholder token" in result.stderr
