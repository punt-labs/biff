"""Round-3 hardening: canary scope, reaper input, restart policy, mailboxes."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import time
import tomllib
import uuid
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

import biff.local_watch as local_watch
from biff.local_watch import LocalWatch, WatchKeeper
from biff.models import BiffConfig, Message
from biff.relay import LocalRelay
from biff.server import app, poller_supervisor
from biff.server.app import create_server
from biff.server.poller_supervisor import PollerSupervisor
from biff.server.state import ServerState
from biff.server.tools import _descriptions
from tests._state import create_state

if TYPE_CHECKING:
    from collections.abc import Callable


def _state(tmp_path: Path) -> ServerState:
    return create_state(
        BiffConfig(user="kai", repo_name="_round3"),
        tmp_path / "relay",
        tty="tty1",
        hostname="h",
        pwd="/p",
        unread_path=tmp_path / "status" / "unread.json",
    )


async def _until(check: Callable[[], bool], what: str, limit: float = 15.0) -> None:
    try:
        async with asyncio.timeout(limit):
            while not check():
                await asyncio.sleep(0.02)
    except TimeoutError:
        pytest.fail(f"never happened: {what}")


def _snapshot(directory: Path) -> tuple[list[str], int]:
    return sorted(p.name for p in directory.iterdir()), directory.stat().st_mtime_ns


def _deaf(*_a: object, **_k: object) -> None:
    """Swallow an event, as a stalled watcher would."""


class _DeafKqueue:
    """A kqueue that is alive and never reports anything."""

    is_alive = True

    def __init__(self, *_a: object) -> None:
        """Accept and ignore the path and callback."""

    def stop(self) -> bool:
        """Report that it stopped."""
        return True


class TestTheCanaryStaysPrivate:
    """A liveness check must not wake any other session watching the directory."""

    async def test_a_check_changes_nothing_in_the_shared_directory(
        self, tmp_path: Path
    ) -> None:
        shared = tmp_path / "relay"
        shared.mkdir()
        with LocalWatch(shared, lambda: None) as watch:
            before = _snapshot(shared)
            assert watch.unhealthy_reason(canary_timeout=15.0) is None
            assert _snapshot(shared) == before

    async def test_two_watches_get_their_own_canary(self, tmp_path: Path) -> None:
        shared = tmp_path / "relay"
        shared.mkdir()
        with (
            LocalWatch(shared, lambda: None) as first,
            LocalWatch(shared, lambda: None) as second,
        ):
            assert first.unhealthy_reason(canary_timeout=15.0) is None
            assert second.unhealthy_reason(canary_timeout=15.0) is None
            names = [p.name for p in shared.iterdir() if p.name.startswith(".")]
            assert len(names) == len(set(names)) == 2


class TestReaperAcceptsOnlySentinels:
    async def test_dot_files_and_empty_keys_never_reach_the_relay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinels = tmp_path / "sentinels"
        sentinels.mkdir()
        (sentinels / ".biff-canary").write_text("")
        (sentinels / "kai-empty").write_text("")
        (sentinels / "kai-garbage").write_text("no-colon")
        (sentinels / ".canary-dir").mkdir()

        def sentinels_here(_repo: str) -> Path:
            return sentinels

        monkeypatch.setattr(poller_supervisor, "sentinel_dir", sentinels_here)
        state = _state(tmp_path)
        get = AsyncMock(return_value=None)
        delete = AsyncMock()
        monkeypatch.setattr(state.relay, "get_session", get)
        monkeypatch.setattr(state.relay, "delete_session", delete)
        await poller_supervisor.reap_sentinels(state)
        get.assert_not_awaited()
        delete.assert_not_awaited()


class TestRestartPolicy:
    def test_the_delay_is_capped_and_cannot_overflow(self) -> None:
        assert poller_supervisor.restart_delay(0) == poller_supervisor._RESTART_DELAY
        assert poller_supervisor.restart_delay(5000) == poller_supervisor._RESTART_CAP

    async def test_a_pending_restart_cannot_fire_after_cleanup_began(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        starts: list[int] = []

        async def dying(*_a: object, **_k: object) -> None:
            starts.append(1)
            msg = "poller bug"
            raise RuntimeError(msg)

        monkeypatch.setattr(_descriptions, "poll_inbox", dying)
        monkeypatch.setattr(poller_supervisor, "_RESTART_DELAY", 0.2)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        state = _state(tmp_path)
        live = app._LiveSession()
        PollerSupervisor(live, create_server(state), state).start()
        await asyncio.sleep(0.05)  # the first poller has died; a restart is pending
        live.shutdown.set()  # cleanup begins
        await asyncio.sleep(0.6)
        assert len(starts) == 1

    async def test_repeated_deaths_degrade_the_description_with_few_tracebacks(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        async def dying(*_a: object, **_k: object) -> None:
            msg = "deterministic bug"
            raise RuntimeError(msg)

        monkeypatch.setattr(_descriptions, "poll_inbox", dying)
        monkeypatch.setattr(poller_supervisor, "_RESTART_DELAY", 0.01)
        monkeypatch.setattr(poller_supervisor, "_DEGRADED_AFTER", 3)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        state = _state(tmp_path)
        mcp = create_server(state)
        live = app._LiveSession()
        with caplog.at_level(logging.WARNING, logger="biff.server.poller_supervisor"):
            PollerSupervisor(live, mcp, state).start()
            tool = await mcp.get_tool("read_messages")
            assert tool is not None
            await _until(
                lambda: (tool.description or "").startswith("[biff: notifications"),
                "the description says notifications stopped",
            )
            await asyncio.sleep(0.3)
            live.shutdown.set()
        with_traceback = [r for r in caplog.records if r.exc_info]
        assert len(with_traceback) <= 2  # first death and the escalation


class TestMailboxesKeepWhatTheyCannotParse:
    async def test_marking_read_keeps_an_unparseable_line(self, tmp_path: Path) -> None:
        relay = LocalRelay(tmp_path)
        message = Message(from_user="eric", to_user="kai", body="hi")
        await relay.deliver(message)
        mailbox = tmp_path / "userinbox-kai.jsonl"
        mailbox.write_text(mailbox.read_text() + '{"from a newer biff": true}\n')
        await relay.mark_read_user_inbox("kai", [message.id])
        text = mailbox.read_text()
        assert '{"from a newer biff": true}' in text
        assert not await relay.fetch_user_inbox("kai")  # the message is read

    async def test_an_unparseable_line_is_warned_about_once(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        relay = LocalRelay(tmp_path)
        await relay.deliver(Message(from_user="eric", to_user="kai", body="hi"))
        mailbox = tmp_path / "userinbox-kai.jsonl"
        junk = f"{{not json {uuid.uuid4()}\n"
        mailbox.write_text(mailbox.read_text() + junk)
        with caplog.at_level(logging.WARNING, logger="biff.relay"):
            for _ in range(5):
                await relay.fetch_user_inbox("kai")
        assert len([r for r in caplog.records if "malformed" in r.getMessage()]) == 1


class TestRecoveryIsProven:
    async def test_a_deaf_watch_does_not_log_a_false_recovery(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        shared = tmp_path / "relay"
        shared.mkdir()
        monkeypatch.setattr(local_watch.ArrivalHandler, "on_any_event", _deaf)
        monkeypatch.setattr(local_watch, "DirectoryKqueue", _DeafKqueue)
        monkeypatch.setattr(LocalWatch, "CANARY_SECONDS", 0.5)
        keeper = WatchKeeper((LocalWatch(shared, lambda: None),))
        keeper.start()
        try:
            with caplog.at_level(logging.DEBUG, logger="biff.local_watch"):
                for _ in range(3):
                    keeper.check()
        finally:
            keeper.stop()
        text = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert not any("running again" in m for m in text)
        assert any("canary" in m for m in text)


class TestWatchdogPrivateApiWeRelyOn:
    def test_the_pin_and_the_private_names_agree(self) -> None:
        project = tomllib.loads(Path("pyproject.toml").read_text())["project"]
        pin = project["dependencies"]
        assert "watchdog>=7,<8" in pin
        spec = importlib.util.find_spec("watchdog.observers.inotify_c")
        assert spec is not None
        assert spec.origin is not None
        source = Path(spec.origin).read_text()
        assert "_instance: ClassVar[InotifyFD | None]" in source
        assert "_global_lock" in source
        assert time.monotonic() > 0
