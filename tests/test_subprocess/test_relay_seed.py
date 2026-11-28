"""A real biffd, started with relay settings, seeds every client from them."""

from __future__ import annotations

import socket
import threading
from collections.abc import Generator
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from biff.client.connection import ClientIdentity, DaemonClient, DaemonError

if TYPE_CHECKING:
    from tests.test_subprocess.conftest import Biffd

pytestmark = pytest.mark.subprocess

_TOKEN: Final = "tok-seeded-0123456789"
_OTHER_TOKEN: Final = "tok-other-9876543210"


class _Relay:
    """A listener standing in for a relay: it counts the connections it gets."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self.accepted = 0

    @property
    def url(self) -> str:
        return f"nats://127.0.0.1:{self._sock.getsockname()[1]}"

    def serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.accepted += 1
            conn.close()


@pytest.fixture
def relay() -> Generator[_Relay]:
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
def biffd_environment(relay: _Relay) -> dict[str, str]:
    return {"BIFF_RELAY_URL": relay.url, "BIFF_RELAY_TOKEN": _TOKEN}


def _identity(repo: Path, session_id: str) -> ClientIdentity:
    return ClientIdentity(
        client="cli", session_id=session_id, cwd=str(repo), user="kai"
    )


def _client_env(monkeypatch: pytest.MonkeyPatch, url: str, token: str) -> None:
    monkeypatch.setenv("BIFF_RELAY_URL", url)
    monkeypatch.setenv("BIFF_RELAY_TOKEN", token)


def _cmdline(pid: int) -> str:
    return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()


def test_the_secret_is_not_on_biffds_command_line(biffd: Biffd) -> None:
    assert _TOKEN not in _cmdline(biffd.proc.pid)


async def test_a_client_with_the_same_environment_reaches_the_seeded_relay(
    biffd: Biffd, relay: _Relay, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both clients pass the comparison; biffd dials the relay it was started with."""
    _client_env(monkeypatch, relay.url, _TOKEN)
    for session_id in ("s-one", "s-two"):
        with pytest.raises(DaemonError) as unreachable:
            await DaemonClient.connect(
                run_dir=biffd.run_dir, identity=_identity(repo, session_id)
            )
        assert "BIFF_RELAY" not in str(unreachable.value)
    assert relay.accepted >= 1


async def test_a_client_with_no_relay_environment_reaches_the_seeded_relay(
    biffd: Biffd, relay: _Relay, repo: Path
) -> None:
    with pytest.raises(DaemonError) as unreachable:
        await DaemonClient.connect(
            run_dir=biffd.run_dir, identity=_identity(repo, "s-none")
        )
    assert "BIFF_RELAY" not in str(unreachable.value)
    assert relay.accepted >= 1


async def test_a_client_with_another_token_is_refused_before_any_relay_work(
    biffd: Biffd, relay: _Relay, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _client_env(monkeypatch, relay.url, _OTHER_TOKEN)
    with pytest.raises(DaemonError, match="BIFF_RELAY_TOKEN") as refused:
        await DaemonClient.connect(
            run_dir=biffd.run_dir, identity=_identity(repo, "s-third")
        )
    assert (_TOKEN in str(refused.value), _OTHER_TOKEN in str(refused.value)) == (
        False,
        False,
    )
    assert relay.accepted == 0


async def test_no_log_line_carries_a_token(
    biffd: Biffd, relay: _Relay, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _client_env(monkeypatch, relay.url, _OTHER_TOKEN)
    with pytest.raises(DaemonError):
        await DaemonClient.connect(
            run_dir=biffd.run_dir, identity=_identity(repo, "s-logged")
        )
    log = biffd.home / ".punt-labs" / "biff" / "logs" / "biff.log"
    text = log.read_text()
    assert ("BIFF_RELAY_TOKEN" in text, _TOKEN in text, _OTHER_TOKEN in text) == (
        True,
        False,
        False,
    )
