"""A harness session ends through biffd: the four effects, then the record.

The obligations are those of ``docs/specs/claude-session-end.tex``: no row, tty
name or record outlives the end (``EndInv``), an end loses no mail (the held
batch is filed back first), only a connection of the current run can end the
session (``EndRefused``), and a clear ends the old session before the new hello.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from biff.client.connection import ConnectionLostError, DaemonError
from biff.frame_reply import ErrorCode
from tests.test_daemon.claude_seats import (
    ConsumingLogging,
    LoggingRelay,
    logging_relay,
    seat,
)

if TYPE_CHECKING:
    from tests.test_daemon.conftest import Harness

_ = logging_relay  # the autouse fixture, imported so pytest finds it here


async def test_ending_a_session_removes_its_row_and_frees_its_name(
    harness: Harness,
) -> None:
    s = await seat(harness, "s-end")
    assert (await s.row_present(), await s.owner()) == (True, s.key)
    await s.client.request("end")
    assert (await s.row_present(), await s.owner()) == (False, None)


async def test_ending_a_session_appends_a_logout(harness: Harness) -> None:
    s = await seat(harness, "s-logout")
    await s.client.request("end")
    logouts = [e for e in LoggingRelay.events if e.event == "logout"]
    assert [e.session_key for e in logouts] == [s.key]


async def test_ending_a_session_forgets_its_record(harness: Harness) -> None:
    s = await seat(harness, "s-forget")
    await s.client.request("end")
    assert harness.daemon.status().sessions == ()


async def test_an_ended_session_answers_no_more_requests(harness: Harness) -> None:
    s = await seat(harness, "s-after")
    await s.client.request("end")
    with pytest.raises(DaemonError) as caught:
        await s.client.request("read")
    assert caught.value.code is ErrorCode.SESSION_ENDED


async def test_a_replaced_run_cannot_end_the_session(harness: Harness) -> None:
    old = await seat(harness, "s-runs", run="a1")
    new = await harness.connect("s-runs", "kai", client="mcp", main_agent_id="a2")
    # The daemon closes a replaced run's connection; a request that beats the
    # close is answered session_replaced.  Either way nothing is ended.
    with pytest.raises((DaemonError, ConnectionLostError)) as caught:
        await old.client.request("end")
    if isinstance(caught.value, DaemonError):
        assert caught.value.code is ErrorCode.SESSION_REPLACED
    assert (await old.row_present(), await old.owner()) == (True, old.key)
    assert len(harness.daemon.status().sessions) == 1
    await new.request("end")  # the current run can
    assert await old.row_present() is False


async def test_a_shells_session_is_not_ended_by_end(harness: Harness) -> None:
    shell = await harness.connect("s-shell", "kai", client="cli")
    with pytest.raises(DaemonError) as caught:
        await shell.request("end")
    assert caught.value.code is ErrorCode.INVALID_PARAMS


async def test_an_end_that_fails_keeps_the_record_and_the_next_one_finishes(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await seat(harness, "s-retry")
    real = LoggingRelay.delete_session
    failures = iter([ConnectionError("relay down")])

    async def flaky(self: LoggingRelay, session_key: str) -> None:
        if (exc := next(failures, None)) is not None:
            raise exc
        await real(self, session_key)

    monkeypatch.setattr(LoggingRelay, "delete_session", flaky)
    with pytest.raises(DaemonError):
        await s.client.request("end")
    # The record stays until every effect is done: still attached, still readable.
    assert len(harness.daemon.status().sessions) == 1
    await s.client.request("read")
    await s.client.request("end")
    assert (await s.row_present(), await s.owner()) == (False, None)
    assert harness.daemon.status().sessions == ()


async def test_a_name_another_session_took_is_not_freed_by_the_end(
    harness: Harness,
) -> None:
    s = await seat(harness, "s-old")
    await s.viewer.release_tty_name("kai", s.tty)
    await s.viewer.reserve_tty_name("kai", s.tty, "kai:someone-else")
    await s.client.request("end")
    assert await s.owner() == "kai:someone-else"


async def test_a_clear_ends_the_old_session_before_the_new_hello(
    harness: Harness,
) -> None:
    old = await seat(harness, "s-before")
    new = await harness.connect("s-after", "kai", client="mcp")
    taken = cast("dict[str, object]", await new.request("tty", name=old.tty))
    assert taken["error"] is True  # the old session still holds the name
    await old.client.request("end")
    taken = cast("dict[str, object]", await new.request("tty", name=old.tty))
    assert taken["error"] is False
    assert await old.owner() is not None
    assert await old.owner() != old.key


async def test_an_end_files_back_the_mail_the_session_holds(harness: Harness) -> None:
    harness.factory.relay_type = ConsumingLogging
    s = await seat(harness, "s-held")
    sender = await harness.connect("s-eric", "eric", client="mcp")
    await sender.request("write", to=f"kai:{s.tty}", message="unconfirmed")
    await s.client.request_held("read")  # fetched, its reply never confirmed
    await s.client.request("end")
    mailbox = [m.body for m in await s.viewer.fetch_user_inbox("kai")]
    assert mailbox == ["unconfirmed"]


async def test_an_end_loses_no_mail_still_in_the_inbox(harness: Harness) -> None:
    s = await seat(harness, "s-inbox")
    sender = await harness.connect("s-eric", "eric", client="mcp")
    await sender.request("write", to=f"kai:{s.tty}", message="first")
    await sender.request("write", to=f"kai:{s.tty}", message="second")
    await s.client.request("end")
    mailbox = {m.body for m in await s.viewer.fetch_user_inbox("kai")}
    assert mailbox == {"first", "second"}
