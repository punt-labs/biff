"""``biff watch`` prints one JSON line per arrival summary biffd pushes."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from typing import TYPE_CHECKING, cast

from biff.cli_daemon import DaemonCommand
from biff.cli_watch import WatchCommand

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    from tests.test_daemon.conftest import Harness

_WAIT = 5.0


class Lines:
    """Collects the lines a watcher prints and lets a test wait for the next."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self.seen: list[dict[str, object]] = []

    def __call__(self, line: str) -> None:
        self._queue.put_nowait(line)

    async def next(self) -> dict[str, object]:
        line = await asyncio.wait_for(self._queue.get(), timeout=_WAIT)
        parsed = cast("dict[str, object]", json.loads(line))
        self.seen.append(parsed)
        return parsed


def _watcher(harness: Harness, out: Callable[[str], None]) -> WatchCommand:
    command = DaemonCommand(
        user_override="kai",
        session_ref=None,
        run_dir=harness.run_dir,
        nudge=lambda: None,
    )
    return WatchCommand(command, out)


async def test_a_message_arrives_as_a_mail_line(harness: Harness, repo: Path) -> None:
    lines = Lines()
    task = asyncio.create_task(_watcher(harness, lines).run())
    first = await lines.next()
    eric = await harness.connect("s-eric", "eric", cwd=str(repo))
    await eric.request("write", to="kai", message="secret body")
    second = await lines.next()
    task.cancel()
    assert (first["kind"], first["data"]) == ("unread", {"count": 0})
    assert second["kind"] == "mail"
    assert "secret body" not in json.dumps(second)


async def test_a_wall_arrives_as_a_wall_line(harness: Harness, repo: Path) -> None:
    eric = await harness.connect("s-eric", "eric", cwd=str(repo))
    await eric.request("wall", message="deploy freeze", duration="1h")
    lines = Lines()
    task = asyncio.create_task(_watcher(harness, lines).run())
    kinds = [(await lines.next())["kind"], (await lines.next())["kind"]]
    task.cancel()
    assert kinds == ["unread", "wall"]


async def test_each_line_is_seq_kind_and_data_only(
    harness: Harness, repo: Path
) -> None:
    lines = Lines()
    task = asyncio.create_task(_watcher(harness, lines).run())
    first = await lines.next()
    task.cancel()
    assert sorted(first) == ["data", "kind", "seq"]


async def test_two_watchers_both_get_every_event(harness: Harness, repo: Path) -> None:
    one, two = Lines(), Lines()
    tasks = [
        asyncio.create_task(_watcher(harness, one).run()),
        asyncio.create_task(_watcher(harness, two).run()),
    ]
    await one.next()
    await two.next()
    eric = await harness.connect("s-eric", "eric", cwd=str(repo))
    await eric.request("write", to="kai", message="x")
    got = [(await one.next())["kind"], (await two.next())["kind"]]
    for task in tasks:
        task.cancel()
    assert got == ["mail", "mail"]


async def test_sigterm_ends_the_watch_with_exit_zero(
    harness: Harness, repo: Path
) -> None:
    lines = Lines()
    task = asyncio.create_task(_watcher(harness, lines).run())
    await lines.next()
    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(task, timeout=_WAIT) == 0


async def test_a_closed_stdout_ends_the_watch_quietly_with_exit_zero(
    harness: Harness, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def closed(line: str) -> None:
        raise BrokenPipeError

    code = await asyncio.wait_for(_watcher(harness, closed).run(), timeout=_WAIT)
    assert (code, capsys.readouterr().err) == (0, "")


async def test_a_lost_connection_exits_one_and_says_why(
    harness: Harness, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = Lines()
    task = asyncio.create_task(_watcher(harness, lines).run())
    await lines.next()
    await harness.daemon.stop()
    code = await asyncio.wait_for(task, timeout=_WAIT)
    assert code == 1
    assert capsys.readouterr().err.startswith("biffd connection lost: ")


async def test_a_daemon_that_is_down_exits_one_on_stderr(
    harness: Harness, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def down() -> None:
        msg = "biffd is not answering"
        raise ConnectionError(msg)

    command = DaemonCommand(
        user_override="kai", session_ref=None, run_dir=harness.run_dir, nudge=down
    )
    code = await WatchCommand(command, Lines()).run()
    captured = capsys.readouterr()
    assert (code, captured.out, captured.err) == (
        1,
        "",
        "Error: biffd is not answering\n",
    )
