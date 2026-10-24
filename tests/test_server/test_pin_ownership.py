"""Real process boundaries for pin ownership, cleanup, and crash recovery."""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from biff.ethos_session import EthosSessionPin
from biff.server.app import _write_sentinel

SID = "ses_ownership123"

# An in-memory MCP client still starts the actual server lifespan, relay
# registration, poller, and teardown in an independent OS process.
_SERVER = """
import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch
from fastmcp import Client
from biff.ethos_session import EthosSessionPin
from biff.models import BiffConfig
from biff.server import app
from biff.server.state import create_state

pin = EthosSessionPin("ses_ownership123", "ses_ownership123")
home = Path.home()
unread = pin.unread_path(home / "unread")
state = create_state(
    BiffConfig(user=os.environ.get("PERSONA", "rmh"), repo_name="test"), home / "relay",
    tty=pin.routing_id, unread_path=unread,
    ethos_session_pin=pin, auto_companion=False,
)
remove = app._remove_unread_files
wall = app.refresh_wall
async def startup_wall(*args):
    if os.environ.get("STARTUP") == "fail":
        raise RuntimeError("injected startup failure")
    if os.environ.get("STARTUP") == "pause":
        print("REGISTERED", flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    await wall(*args)
def cleanup(path):
    print("CLEANUP", flush=True)
    if os.environ["PAUSE_CLEANUP"] == "1":
        sys.stdin.readline()
    remove(path)

async def main():
    with (
        patch.object(app, "_remove_unread_files", cleanup),
        patch.object(app, "refresh_wall", startup_wall),
    ):
        async with Client(app.create_server(state)):
            print("READY", flush=True)
            await asyncio.to_thread(sys.stdin.readline)
    print("STOPPED", flush=True)

asyncio.run(main())
"""


@contextmanager
def _server(
    home: Path,
    *,
    pause_cleanup: bool = True,
    startup: str = "normal",
    persona: str = "rmh",
) -> Generator[subprocess.Popen[str]]:
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-u", "-c", _SERVER],
        env=dict(
            os.environ,
            HOME=str(home),
            PAUSE_CLEANUP=str(int(pause_cleanup)),
            STARTUP=startup,
            PERSONA=persona,
        ),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=15)


def _expect(process: subprocess.Popen[str], line: str) -> None:
    assert process.stdout is not None
    assert select.select([process.stdout], [], [], 15)[0], f"waiting for {line}"
    actual = process.stdout.readline().strip()
    if not actual:
        assert process.stderr is not None
        pytest.fail(process.stderr.read())
    assert actual == line


def _advance(process: subprocess.Popen[str]) -> None:
    assert process.stdin is not None
    process.stdin.write("continue\n")
    process.stdin.flush()


def _assert_overlap_rejected(home: Path) -> None:
    with _server(home) as replacement:
        _, stderr = replacement.communicate(timeout=15)
        assert replacement.returncode != 0
        assert "already has a running Biff process" in stderr


def test_overlap_excluded_through_unread_and_relay_cleanup(tmp_path: Path) -> None:
    pin = EthosSessionPin(SID, SID)
    unread = pin.unread_path(tmp_path / "unread")
    sidecar = unread.with_suffix(".nudged")
    sessions = tmp_path / "relay" / "sessions.json"
    with _server(tmp_path) as owner:
        _expect(owner, "READY")
        sidecar.write_text("owned")
        original_unread = unread.read_bytes()
        original_sessions = sessions.read_bytes()
        _assert_overlap_rejected(tmp_path)
        assert unread.read_bytes() == original_unread
        assert sessions.read_bytes() == original_sessions
        assert sidecar.read_text() == "owned"
        _advance(owner)
        _expect(owner, "CLEANUP")
        _assert_overlap_rejected(tmp_path)
        assert unread.exists() and sidecar.exists()
        _advance(owner)
        _expect(owner, "STOPPED")
        assert owner.wait(timeout=15) == 0
    assert not unread.exists() and not sidecar.exists()
    assert json.loads(sessions.read_text()) == {}
    with _server(tmp_path) as resumed:
        _expect(resumed, "READY")
        rows = json.loads(sessions.read_text())
        assert list(rows) == [f"rmh:{pin.routing_id}"]
        assert unread.exists()
        _advance(resumed)
        _expect(resumed, "CLEANUP")
        _advance(resumed)
        _expect(resumed, "STOPPED")


_REAPER = """
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch
from biff.models import BiffConfig
from biff.server import app
from biff.server.state import create_state

state = create_state(BiffConfig(user="foreign", repo_name="test"), Path.home()/"relay")
reap = app._reap_dead_session
async def paused(*args):
    print("REAP_LOCKED", flush=True)
    await asyncio.to_thread(sys.stdin.readline)
    return await reap(*args)
async def main():
    if sys.argv[1] == "pause":
        with patch.object(app, "_reap_dead_session", paused):
            await app._reap_sentinels(state)
    else:
        await app._reap_sentinels(state)
        state.relay.reap_sentinels()
    print("REAP_DONE", flush=True)
asyncio.run(main())
"""


@contextmanager
def _reaper(home: Path, *, paused: bool) -> Generator[subprocess.Popen[str]]:
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-u", "-c", _REAPER, "pause" if paused else "normal"],
        env=dict(os.environ, HOME=str(home)),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=15)


def test_foreign_reaper_cannot_delete_registered_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    pin = EthosSessionPin(SID, SID)
    key = f"rmh:{pin.routing_id}"
    _write_sentinel("test", key)
    with _server(tmp_path, startup="pause") as owner:
        _expect(owner, "REGISTERED")
        # Also exercise a sentinel arriving after startup consumption, and
        # LocalRelay's independent remove-* reaping path.
        _write_sentinel("test", key)
        (tmp_path / "relay" / f"remove-rmh-{pin.routing_id}").write_text(key)
        with _reaper(tmp_path, paused=False) as foreign:
            _expect(foreign, "REAP_DONE")
            assert foreign.wait(timeout=15) == 0
        rows = json.loads((tmp_path / "relay/sessions.json").read_text())
        assert list(rows) == [key]
        _advance(owner)
        _expect(owner, "READY")
        rows = json.loads((tmp_path / "relay/sessions.json").read_text())
        assert list(rows) == [key]
        _advance(owner)
        _expect(owner, "CLEANUP")
        _advance(owner)
        _expect(owner, "STOPPED")


def test_inflight_foreign_reaper_excludes_replacement_until_delete_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    pin = EthosSessionPin(SID, SID)
    # Seed the crashed predecessor's row and lock inode.
    with _server(tmp_path, pause_cleanup=False) as predecessor:
        _expect(predecessor, "READY")
        predecessor.kill()
        predecessor.wait(timeout=15)
    _write_sentinel("test", f"rmh:{pin.routing_id}")
    with _reaper(tmp_path, paused=True) as foreign:
        _expect(foreign, "REAP_LOCKED")
        _assert_overlap_rejected(tmp_path)
        _advance(foreign)
        _expect(foreign, "REAP_DONE")
        assert foreign.wait(timeout=15) == 0
    with _server(tmp_path) as resumed:
        _expect(resumed, "READY")
        rows = json.loads((tmp_path / "relay/sessions.json").read_text())
        assert list(rows) == [f"rmh:{pin.routing_id}"]
        _advance(resumed)
        _expect(resumed, "CLEANUP")
        _advance(resumed)
        _expect(resumed, "STOPPED")


def test_startup_failure_cleans_before_next_owner(tmp_path: Path) -> None:
    pin = EthosSessionPin(SID, SID)
    with _server(tmp_path, pause_cleanup=False, startup="fail") as failed:
        _, stderr = failed.communicate(timeout=15)
        assert failed.returncode != 0
        assert "injected startup failure" in stderr
    assert not pin.unread_path(tmp_path / "unread").exists()
    assert json.loads((tmp_path / "relay/sessions.json").read_text()) == {}
    assert not list((tmp_path / ".punt-labs/biff/active").iterdir())
    with _server(tmp_path) as resumed:
        _expect(resumed, "READY")
        _advance(resumed)
        _expect(resumed, "CLEANUP")
        _advance(resumed)
        _expect(resumed, "STOPPED")


@pytest.mark.parametrize("contents", ["", "malformed", "other:../unsafe"])
def test_unrelated_corrupt_sentinel_does_not_block_pinned_startup(
    tmp_path: Path, contents: str
) -> None:
    directory = tmp_path / "relay"
    directory.mkdir()
    invalid = directory / "remove-unrelated"
    invalid.write_text(contents)
    with _server(tmp_path) as owner:
        _expect(owner, "READY")
        assert not invalid.exists()
        rows = json.loads((directory / "sessions.json").read_text())
        pin = EthosSessionPin(SID, SID)
        assert list(rows) == [f"rmh:{pin.routing_id}"]
        _advance(owner)
        _expect(owner, "CLEANUP")
        _advance(owner)
        _expect(owner, "STOPPED")


def test_persona_change_retires_both_predecessor_sentinels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    pin = EthosSessionPin(SID, SID)
    with _server(tmp_path, pause_cleanup=False) as predecessor:
        _expect(predecessor, "READY")
        predecessor.kill()
        predecessor.wait(timeout=15)
    key = f"rmh:{pin.routing_id}"
    _write_sentinel("test", key)
    local = tmp_path / "relay" / f"remove-rmh-{pin.routing_id}"
    local.write_text(key)
    # An unrelated routing key must not be consumed by predecessor retirement.
    unrelated = tmp_path / "relay" / "remove-unrelated-abcdef1234567890"
    unrelated.write_text("unrelated:abcdef1234567890")
    with _server(tmp_path, persona="gvr") as replacement:
        _expect(replacement, "READY")
        rows = json.loads((tmp_path / "relay/sessions.json").read_text())
        assert list(rows) == [f"gvr:{pin.routing_id}"]
        assert not local.exists()
        assert not (
            tmp_path / ".punt-labs/biff/sentinels/test" / f"rmh-{pin.routing_id}"
        ).exists()
        assert not (
            tmp_path / ".punt-labs/biff/active" / f"rmh-{pin.routing_id}"
        ).exists()
        _advance(replacement)
        _expect(replacement, "CLEANUP")
        _advance(replacement)
        _expect(replacement, "STOPPED")


@pytest.mark.parametrize("signal_exit", [False, True])
def test_process_death_releases_pin_and_resume_keeps_presence(
    tmp_path: Path, signal_exit: bool
) -> None:
    pin = EthosSessionPin(SID, SID)
    with _server(tmp_path, pause_cleanup=False) as owner:
        _expect(owner, "READY")
        if signal_exit:
            owner.terminate()
        else:
            owner.kill()
        assert owner.wait(timeout=15) != 0
    with _server(tmp_path) as resumed:
        _expect(resumed, "READY")
        rows = json.loads((tmp_path / "relay" / "sessions.json").read_text())
        assert list(rows) == [f"rmh:{pin.routing_id}"]
        assert pin.unread_path(tmp_path / "unread").exists()
        _advance(resumed)
        _expect(resumed, "CLEANUP")
        _advance(resumed)
        _expect(resumed, "STOPPED")
