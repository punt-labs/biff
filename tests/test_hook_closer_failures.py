"""What the SessionEnd closer does when biffd cannot, or may not, end the session."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, cast

import pytest

from biff.client.connection import (
    ClientIdentity,
    ConnectionLostError,
    DaemonClient,
    DaemonError,
)
from biff.frame_reply import ErrorCode
from biff.plugins.claude_code.daemon_session_end import DaemonSessionCloser
from biff.plugins.claude_code.hook_daemon import HookDaemon
from biff.session_handle import SessionHandleStore
from biff.session_routing import SessionRouting

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

_PAYLOAD: dict[str, object] = {"session_id": "sess-1", "cwd": "/work/repo"}


class _Client:
    """A connected client whose ``end`` request fails as configured."""

    def __init__(self, failure: Exception | None) -> None:
        self._failure = failure

    async def request(self, method: str) -> None:
        assert method == "end"
        if self._failure is not None:
            raise self._failure

    async def close(self) -> None:
        return None


def _daemon(
    tmp_path: Path,
    connect: Callable[[ClientIdentity], Awaitable[DaemonClient]],
    *,
    deadline: float = 5.0,
    started: float | None = None,
) -> HookDaemon:
    return HookDaemon(
        connect=connect,
        unread_dir=tmp_path / "unread",
        handles=SessionHandleStore(tmp_path / "sessions"),
        deadline=deadline,
        started=time.time() if started is None else started,
    )


def _refusing(
    failure: Exception,
) -> Callable[[ClientIdentity], Awaitable[DaemonClient]]:
    async def connect(_identity: ClientIdentity) -> DaemonClient:
        raise failure

    return connect


def _connecting(
    failure: Exception | None,
) -> Callable[[ClientIdentity], Awaitable[DaemonClient]]:
    async def connect(_identity: ClientIdentity) -> DaemonClient:
        return cast("DaemonClient", _Client(failure))

    return connect


def _leave_handle(tmp_path: Path) -> SessionHandleStore:
    handles = SessionHandleStore(tmp_path / "sessions")
    handles.write(SessionRouting("sess-1"), "kai:abc")
    return handles


@pytest.mark.parametrize(
    "failure",
    [
        ConnectionLostError("biffd closed the connection"),
        ConnectionError("biffd is not installed"),
        TimeoutError(),
        DaemonError(ErrorCode.BAD_HELLO, "an older biffd does not know ends"),
        DaemonError(ErrorCode.SESSION_ENDED, "biffd holds no session sess-1"),
    ],
)
async def test_a_hello_biffd_refuses_is_logged_and_left_to_the_fallback(
    tmp_path: Path, failure: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    handles = _leave_handle(tmp_path)
    closer = DaemonSessionCloser(_daemon(tmp_path, _refusing(failure)))

    with caplog.at_level(logging.WARNING):
        settled = await closer.end(_PAYLOAD)

    assert settled is False
    assert handles.read(SessionRouting("sess-1")) == "kai:abc"  # nothing wiped
    assert "could not end session sess-1" in caplog.text


async def test_a_refusal_for_a_newer_run_is_a_warning_and_leaves_the_files(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    handles = _leave_handle(tmp_path)
    refusal = DaemonError(ErrorCode.SESSION_REPLACED, "run 1 is not the current run")
    closer = DaemonSessionCloser(_daemon(tmp_path, _refusing(refusal)))

    with caplog.at_level(logging.WARNING):
        settled = await closer.end(_PAYLOAD)

    assert settled is True  # a newer run owns the session; no cleanup may touch it
    assert handles.read(SessionRouting("sess-1")) == "kai:abc"
    assert "newer run" in caplog.text


async def test_a_connection_lost_mid_end_is_uncertain_so_no_reaper_and_no_wipe(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    handles = _leave_handle(tmp_path)
    lost = ConnectionLostError("biffd closed the connection")
    closer = DaemonSessionCloser(_daemon(tmp_path, _connecting(lost)))

    with caplog.at_level(logging.WARNING):
        settled = await closer.end(_PAYLOAD)

    # biffd may have finished the end, or a newer run may own the session: the
    # sentinel reaper could reap that run's row, so it does not run.
    assert settled is True
    assert handles.read(SessionRouting("sess-1")) == "kai:abc"
    assert "may or may not have ended" in caplog.text


async def test_a_session_biffd_reports_already_ended_is_cleared(
    tmp_path: Path,
) -> None:
    handles = _leave_handle(tmp_path)
    ended = DaemonError(ErrorCode.SESSION_ENDED, "session sess-1 ended")
    closer = DaemonSessionCloser(_daemon(tmp_path, _connecting(ended)))

    assert await closer.end(_PAYLOAD) is True
    assert handles.read(SessionRouting("sess-1")) is None


async def test_the_deadline_counts_the_time_the_process_took_to_start(
    tmp_path: Path,
) -> None:
    async def slow(_identity: ClientIdentity) -> DaemonClient:
        await asyncio.sleep(30)
        raise AssertionError

    # The process started 100 s ago: the budget is gone, so only the floor is left.
    daemon = _daemon(tmp_path, slow, deadline=8.0, started=time.time() - 100)
    start = time.monotonic()

    assert await DaemonSessionCloser(daemon).end(_PAYLOAD) is False

    assert time.monotonic() - start < 5.0
