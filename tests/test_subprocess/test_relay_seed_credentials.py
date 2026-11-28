"""biffd sends the credentials it was started with only to its own relay."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
from collections.abc import Generator
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from biff.client.connection import (
    ClientIdentity,
    ConnectionLostError,
    DaemonClient,
    DaemonError,
)

if TYPE_CHECKING:
    from tests.test_subprocess.conftest import Biffd

pytestmark = pytest.mark.subprocess

_TOKEN: Final = "tok-seeded-0123456789"
_INFO: Final = (
    b'INFO {"server_id":"x","version":"2.10.0","proto":1,"max_payload":1048576}\r\n'
)


class _Relay:
    """A listener that speaks just enough NATS to be sent a CONNECT line."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self.received = b""

    @property
    def url(self) -> str:
        return f"nats://127.0.0.1:{self._sock.getsockname()[1]}"

    def serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                conn.sendall(_INFO)
                conn.settimeout(2.0)
                try:
                    self.received += conn.recv(65536)
                except OSError:
                    continue


@pytest.fixture
def relay() -> Generator[_Relay]:
    """A relay a repository's committed configuration names."""
    yield from _listening()


@pytest.fixture
def seeded_relay() -> Generator[_Relay]:
    """The relay biffd is started with."""
    yield from _listening()


def _listening() -> Generator[_Relay]:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        standin = _Relay(sock)
        thread = threading.Thread(target=standin.serve, daemon=True)
        thread.start()
        yield standin
        sock.close()
        thread.join(timeout=5)


@pytest.fixture
def biffd_environment(seeded_relay: _Relay) -> dict[str, str]:
    return {"BIFF_RELAY_URL": seeded_relay.url, "BIFF_RELAY_TOKEN": _TOKEN}


@pytest.fixture(autouse=True)
def _same_environment(seeded_relay: _Relay, monkeypatch: pytest.MonkeyPatch) -> None:
    """The client runs in the environment biffd was started in."""
    monkeypatch.setenv("BIFF_RELAY_URL", seeded_relay.url)
    monkeypatch.setenv("BIFF_RELAY_TOKEN", _TOKEN)


def _commit_relay(repo: Path, url: str, *, allow_override: bool) -> None:
    config = repo / ".punt-labs" / "biff" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    allow = "  allow_env_override: true\n" if allow_override else ""
    config.write_text(f"relay:\n  url: {url}\n{allow}", encoding="utf-8")


async def _open(biffd: Biffd, repo: Path) -> None:
    with pytest.raises((DaemonError, ConnectionLostError)):  # no session finishes
        await DaemonClient.connect(
            run_dir=biffd.run_dir,
            identity=ClientIdentity(
                client="cli", session_id="s-pull", cwd=str(repo), user="kai"
            ),
        )


@pytest.mark.parametrize("allow_override", [True, False])
async def test_a_repository_cannot_pull_the_seeded_token_to_its_own_relay(
    biffd: Biffd, relay: _Relay, seeded_relay: _Relay, repo: Path, allow_override: bool
) -> None:
    _commit_relay(repo, relay.url, allow_override=allow_override)
    await _open(biffd, repo)
    assert _TOKEN.encode() not in relay.received


async def test_the_seeded_token_goes_to_the_seeded_relay(
    biffd: Biffd, seeded_relay: _Relay, repo: Path
) -> None:
    await _open(biffd, repo)
    assert _TOKEN.encode() in seeded_relay.received


@pytest.mark.parametrize(
    "variable", ["BIFF_RELAY_TOKEN", "BIFF_RELAY_USER_CREDENTIALS"]
)
def test_credentials_without_a_url_stop_biffd_at_start(
    variable: str, tmp_path: Path
) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("BIFF_RELAY_")}
    env |= {"HOME": str(tmp_path), "PUNT_TELEMETRY": "off"}
    env[variable] = _TOKEN
    started = subprocess.run(
        [sys.executable, "-m", "biff", "daemon", "serve", "--port", "0"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert (started.returncode != 0, variable in started.stderr) == (True, True)
    assert _TOKEN not in started.stderr + started.stdout
