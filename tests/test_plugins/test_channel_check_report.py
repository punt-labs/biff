"""``biff doctor``'s end-to-end channel check, with a fake relay (DES-087)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from biff.models import UserSession
from biff.plugins.claude_code.channel_check_report import (
    ChannelCheckReport,
    CheckTarget,
)

_NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


class _Pending:
    def __init__(self, *, answers: bool, delay: float = 0.0) -> None:
        self._answers = answers
        self._delay = delay
        self.closed = 0

    async def confirmed(self, timeout: float) -> bool:
        await asyncio.sleep(min(self._delay, timeout))
        return self._answers and self._delay <= timeout

    async def close(self) -> None:
        self.closed += 1


class _Relay:
    """A relay that knows its sessions and which of them answer."""

    def __init__(
        self,
        sessions: list[UserSession],
        *,
        answering: set[str],
        fails: Exception | None = None,
    ) -> None:
        self._sessions = sessions
        self._answering = answering
        self._fails = fails
        self.requested: list[str] = []
        self.pending: list[_Pending] = []
        self.closed = 0

    async def get_sessions_for_user(self, user: str) -> list[UserSession]:
        if self._fails is not None:
            raise self._fails
        return [s for s in self._sessions if s.user == user]

    async def request_channel_check(self, session_key: str) -> _Pending:
        self.requested.append(session_key)
        pending = _Pending(answers=session_key in self._answering)
        self.pending.append(pending)
        return pending

    async def close(self) -> None:
        self.closed += 1


def _session(tty: str, *, name: str = "", age: float = 0.0) -> UserSession:
    return UserSession(
        user="kai",
        tty=tty,
        tty_name=name,
        last_active=_NOW - timedelta(seconds=age),
    )


def _report(relay: _Relay | None, *, timeout: float = 30.0) -> ChannelCheckReport:
    return ChannelCheckReport(
        CheckTarget(relay, "kai"), timeout=timeout, now=lambda: _NOW
    )


async def test_a_session_that_answers_is_channel_live() -> None:
    relay = _Relay([_session("abc", name="tty1")], answering={"kai:abc"})
    (line,) = await _report(relay).results()
    assert line.message == "kai:tty1: channel live"
    assert line.passed
    assert not line.required  # doctor never fails on a channel


async def test_a_session_that_does_not_answer_points_at_the_gates() -> None:
    relay = _Relay([_session("abc", name="tty1")], answering=set())
    (line,) = await _report(relay, timeout=0.05).results()
    assert line.message.startswith("kai:tty1: channel not confirmed")
    assert "Channels" in line.message  # points at the gate checks, not their text
    assert not line.passed
    assert not line.required


async def test_each_session_gets_its_own_line_and_all_are_asked_together() -> None:
    relay = _Relay(
        [_session("a", name="tty1"), _session("b", name="tty2")],
        answering={"kai:a"},
    )
    lines = await _report(relay, timeout=0.05).results()
    assert sorted(relay.requested) == ["kai:a", "kai:b"]
    assert [(ln.message.split(":")[1].split()[0], ln.passed) for ln in lines] == [
        ("tty1", True),
        ("tty2", False),
    ]


async def test_every_pending_check_is_closed() -> None:
    relay = _Relay([_session("a"), _session("b")], answering={"kai:a"})
    await _report(relay, timeout=0.05).results()
    assert [p.closed for p in relay.pending] == [1, 1]


async def test_the_relay_is_closed_after_the_check() -> None:
    relay = _Relay([_session("a")], answering=set())
    await _report(relay, timeout=0.05).results()
    assert relay.closed == 1


async def test_a_session_without_a_name_is_named_by_its_tty() -> None:
    relay = _Relay([_session("abc12345")], answering={"kai:abc12345"})
    (line,) = await _report(relay).results()
    assert line.message == "kai:abc12345: channel live"


async def test_a_session_that_stopped_heartbeating_is_not_asked() -> None:
    relay = _Relay([_session("old", age=3600)], answering=set())
    (line,) = await _report(relay).results()
    assert relay.requested == []
    assert "nothing to check" in line.message


async def test_no_sessions_is_one_informational_line() -> None:
    (line,) = await _report(_Relay([], answering=set())).results()
    assert "nothing to check" in line.message
    assert not line.required


async def test_a_relay_that_is_not_nats_says_the_check_needs_it() -> None:
    (line,) = await _report(None).results()
    assert "needs the NATS relay" in line.message
    assert not line.required


@pytest.mark.parametrize("error", [ConnectionError("x"), TimeoutError()])
async def test_an_unreachable_relay_says_the_check_needs_it(error: Exception) -> None:
    relay = _Relay([], answering=set(), fails=error)
    (line,) = await _report(relay).results()
    assert "needs the NATS relay" in line.message
    assert relay.closed == 1


async def test_the_wait_is_bounded_by_the_timeout() -> None:
    relay = _Relay([_session("a")], answering=set())
    started = asyncio.get_running_loop().time()
    await _report(relay, timeout=0.1).results()
    assert asyncio.get_running_loop().time() - started < 2
