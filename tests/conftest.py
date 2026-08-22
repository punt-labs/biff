"""Shared test fixtures for biff."""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from biff.server.tools._descriptions import _reset_session

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _confine_git_walk(  # pyright: ignore[reportUnusedFunction]
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop ``git`` from climbing out of the test's tmp_path into the real repo.

    ``TMPDIR`` points inside the biff repo, so any code that shells out to git
    (e.g. ``git_hooks.resolve_hooks_dir`` via ``enable``/``install``) would
    otherwise walk up and resolve a bare tmp_path to the project's own
    ``.git`` -- reading or even writing the real hooks. The ceiling must be
    tmp_path's *parent*, not tmp_path: git deliberately does not exclude the
    starting directory itself, so a ceiling equal to the cwd is ignored.
    """
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))


@pytest.fixture(autouse=True)
def _clear_claude_session_env(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strip ``CLAUDE_PID`` so tests run under Claude Code itself don't leak
    the real running process's PID into code paths that prefer it
    (``SessionHint.resolve_routing_id``, ``_resolve_claude_pid``). Tests
    that want to exercise the env-var path set it explicitly via
    ``monkeypatch.setenv``.

    Deliberately suite-wide, not scoped to ``test_session_id.py``: this also
    reaches subprocess-tier tests that spawn a real ``biff mcp`` process
    (``subprocess.Popen`` inherits the parent's stripped environment). If a
    second consumer of ``CLAUDE_PID`` is ever added, its tests inherit this
    stripping by default too -- that is the point, not an oversight, so a
    future author debugging a missing env var in an unrelated test should
    look here first.
    """
    monkeypatch.delenv("CLAUDE_PID", raising=False)


@pytest.fixture(autouse=True)
def _reset_description_globals() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Clear ``_descriptions`` module globals around every test.

    ``_SessionCaptureMiddleware`` stores the MCP session in a module
    global on every client ``initialize``, and the ``tty``/``mesg`` tools
    set the tty-name and biff-enabled globals.  Nothing clears them when a
    test ends, so a stale (closed, wrong-event-loop) session leaks into the
    next test's background poller and NATS callbacks — a later test then
    fails only under certain orderings.  Resetting before and after each
    test makes ordering irrelevant.
    """
    _reset_session()
    yield
    _reset_session()


@pytest.fixture(autouse=True)
def _silence_vox(request: pytest.FixtureRequest) -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Prevent vox from speaking during tests.

    Patches ``vox_binary`` to return ``None`` so
    ``speak_fire_and_forget`` is a no-op — no audio output,
    no subprocess spawned, no system state changed.

    Tests marked ``@pytest.mark.vox`` opt out and manage
    vox mocking themselves.
    """
    node = request.node  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
    if node.get_closest_marker("vox"):  # pyright: ignore[reportUnknownMemberType]
        yield
        return
    with patch("biff.integration.vox.vox_binary", return_value=None):
        yield


def _find_free_port() -> int:
    """Find an available TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture(scope="session")
def nats_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Start a nats-server with JetStream for the test session.

    Returns the server URL (e.g. ``nats://127.0.0.1:14222``).
    Shared across all test directories that need a NATS server.

    The JetStream store lives in a per-session temp directory so every
    run starts with an empty KV.  Without an explicit ``--store_dir`` the
    server defaults to ``$TMPDIR/nats/jetstream`` — a fixed path that
    survives across sessions.  There, ``biff-names`` reservations pile up
    forever (``ttyN`` climbs into the thousands, breaking presence tests)
    and the file store eventually corrupts, wedging connections with
    ``Disconnected after 0s connected``.
    """
    exe = shutil.which("nats-server")
    if exe is None:
        pytest.skip("nats-server not found on PATH")

    port = _find_free_port()
    url = f"nats://127.0.0.1:{port}"
    store_dir = tmp_path_factory.mktemp("nats-jetstream")

    proc = subprocess.Popen(  # noqa: S603
        [exe, "-js", "-sd", str(store_dir), "-p", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for server to be ready
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("nats-server did not start within 5 seconds")

    yield url

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
