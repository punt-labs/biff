"""``biff doctor``'s end-to-end channel check, with a fake relay (DES-087)."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest

from biff.models import UserSession
from biff.plugins.claude_code.channel_check_report import ChannelCheckReport
from biff.plugins.claude_code.channel_check_target import CheckTarget

_NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


class _Pending:
    """A check that answers after *delay* seconds, if it answers at all."""

    def __init__(self, delay: float | None) -> None:
        self._delay = delay  # None: never answers
        self.closed = 0

    async def confirmed(self, timeout: float) -> bool:
        if self._delay is None:
            await asyncio.sleep(timeout)
            return False
        if self._delay > timeout:
            await asyncio.sleep(timeout)
            return False
        await asyncio.sleep(self._delay)
        return True

    async def close(self) -> None:
        self.closed += 1


class _Relay:
    """A relay that knows its sessions and how long each takes to answer."""

    def __init__(
        self,
        sessions: list[UserSession],
        *,
        delays: dict[str, float | None] | None = None,
        fails: Exception | None = None,
    ) -> None:
        self._sessions = sessions
        self._delays = delays or {}
        self._fails = fails
        self.requested: list[str] = []
        self.looked_up: list[str] = []
        self.pending: list[_Pending] = []
        self.closed = 0

    async def list_sessions_for_user(self, user: str) -> list[UserSession]:
        self.looked_up.append(user)
        if self._fails is not None:
            raise self._fails
        return [s for s in self._sessions if s.user == user]

    async def request_channel_check(self, session_key: str) -> _Pending:
        self.requested.append(session_key)
        pending = _Pending(self._delays.get(session_key))
        self.pending.append(pending)
        return pending

    async def close(self) -> None:
        self.closed += 1


def _session(
    tty: str, *, name: str = "", age: float = 0.0, user: str = "kai"
) -> UserSession:
    return UserSession(
        user=user,
        tty=tty,
        tty_name=name,
        last_active=_NOW - timedelta(seconds=age),
    )


def _report(
    relay: _Relay | None, *, timeout: float = 30.0, users: tuple[str, ...] = ("kai",)
) -> ChannelCheckReport:
    return ChannelCheckReport(
        CheckTarget(relay, users), timeout=timeout, now=lambda: _NOW
    )


async def test_a_session_that_answers_is_channel_live() -> None:
    relay = _Relay([_session("abc", name="tty1")], delays={"kai:abc": 0.0})
    (line,) = await _report(relay).results()
    assert line.message == "kai:tty1: channel live"
    assert line.passed
    assert not line.required  # doctor never fails on a channel


async def test_a_session_that_does_not_answer_points_at_the_gates() -> None:
    relay = _Relay([_session("abc", name="tty1")])
    (line,) = await _report(relay, timeout=0.05).results()
    assert line.message.startswith("kai:tty1: channel not confirmed")
    assert "Channels" in line.message  # points at the gate checks, not their text
    assert not line.passed
    assert not line.required


async def test_each_session_gets_its_own_line() -> None:
    relay = _Relay(
        [_session("a", name="tty1"), _session("b", name="tty2")],
        delays={"kai:a": 0.0},
    )
    lines = await _report(relay, timeout=0.05).results()
    assert sorted(relay.requested) == ["kai:a", "kai:b"]
    assert [(ln.message.split(":")[1].split()[0], ln.passed) for ln in lines] == [
        ("tty1", True),
        ("tty2", False),
    ]


async def test_sessions_that_take_a_second_are_waited_for_together() -> None:
    """Three 1 s sessions finish in about 1 s: serial checks would take 3 s."""
    sessions = [_session(t) for t in "abc"]
    relay = _Relay(sessions, delays={"kai:a": 1.0, "kai:b": 1.0, "kai:c": 1.0})
    started = time.monotonic()
    lines = await _report(relay, timeout=5).results()
    elapsed = time.monotonic() - started
    assert all(ln.passed for ln in lines)
    assert 0.9 <= elapsed < 2.0


async def test_a_session_that_never_answers_ends_at_the_timeout() -> None:
    relay = _Relay([_session("a"), _session("b")], delays={"kai:a": 0.0})
    started = time.monotonic()
    lines = await asyncio.wait_for(_report(relay, timeout=0.5).results(), 5)
    elapsed = time.monotonic() - started
    assert [ln.passed for ln in lines] == [True, False]
    assert 0.45 <= elapsed < 1.5


async def test_an_answer_later_than_the_timeout_is_not_confirmed() -> None:
    relay = _Relay([_session("a")], delays={"kai:a": 2.0})
    (line,) = await asyncio.wait_for(_report(relay, timeout=0.3).results(), 5)
    assert not line.passed


async def test_every_pending_check_is_closed() -> None:
    relay = _Relay([_session("a"), _session("b")], delays={"kai:a": 0.0})
    await _report(relay, timeout=0.05).results()
    assert [p.closed for p in relay.pending] == [1, 1]


async def test_the_relay_is_closed_after_the_check() -> None:
    relay = _Relay([_session("a")])
    await _report(relay, timeout=0.05).results()
    assert relay.closed == 1


async def test_a_session_without_a_name_is_named_by_its_tty() -> None:
    relay = _Relay([_session("abc12345")], delays={"kai:abc12345": 0.0})
    (line,) = await _report(relay).results()
    assert line.message == "kai:abc12345: channel live"


async def test_a_session_that_stopped_heartbeating_is_not_asked() -> None:
    relay = _Relay([_session("old", age=3600)])
    (line,) = await _report(relay).results()
    assert relay.requested == []
    assert "nothing to check" in line.message


async def test_no_sessions_is_one_informational_line() -> None:
    (line,) = await _report(_Relay([])).results()
    assert "nothing to check" in line.message
    assert not line.required


async def test_a_relay_that_is_not_nats_says_the_check_needs_it() -> None:
    (line,) = await _report(None).results()
    assert "needs the NATS relay" in line.message
    assert not line.required


async def test_an_unreachable_relay_says_the_check_needs_it() -> None:
    relay = _Relay([], fails=ConnectionError("x"))
    (line,) = await _report(relay).results()
    assert "needs the NATS relay" in line.message
    assert relay.closed == 1


@pytest.mark.parametrize("error", [TimeoutError(), OSError("boom")])
async def test_a_failed_lookup_is_not_reported_as_no_session(error: Exception) -> None:
    """A presence lookup that failed must not read as "nothing running"."""
    relay = _Relay([_session("a")], fails=error)
    (line,) = await _report(relay).results()
    assert "could not list" in line.message
    assert "nothing to check" not in line.message
    assert not line.required


# -- every handle an MCP server in the repo would register under ----------------


async def test_every_handle_is_looked_up_and_each_session_is_asked() -> None:
    relay = _Relay(
        [_session("h", user="kai"), _session("a", user="claude")],
        delays={"kai:h": 0.0, "claude:a": 0.0},
    )
    lines = await _report(relay, users=("claude", "kai")).results()
    assert relay.looked_up == ["claude", "kai"]
    assert sorted(ln.message for ln in lines) == [
        "claude:a: channel live",
        "kai:h: channel live",
    ]


# -- a bad handle is one line, never an exception -------------------------------


async def test_a_handle_the_relay_rejects_is_one_informational_line() -> None:
    relay = _Relay([], fails=ValueError("Invalid username: 'first.last'"))
    (line,) = await _report(relay, users=("first.last",)).results()
    assert "first.last" in line.message
    assert "not a valid biff handle" in line.message
    assert not line.required


async def test_a_bad_handle_does_not_hide_the_sessions_of_a_good_one() -> None:
    class _Picky(_Relay):
        async def list_sessions_for_user(self, user: str) -> list[UserSession]:
            if "." in user:
                raise ValueError("Invalid username")
            return await super().list_sessions_for_user(user)

    relay = _Picky([_session("a")], delays={"kai:a": 0.0})
    lines = await _report(relay, users=("first.last", "kai")).results()
    assert [ln.passed for ln in lines] == [False, True]
    assert "not a valid biff handle" in lines[0].message
    assert lines[1].message == "kai:a: channel live"


# -- text from the relay never reaches the terminal raw -------------------------


async def test_escape_sequences_in_a_session_name_are_stripped() -> None:
    evil = "tty\x1b]0;pwned\x07\x1b[2J1"
    relay = _Relay([_session("a", name=evil)], delays={"kai:a": 0.0})
    (line,) = await _report(relay).results()
    assert "\x1b" not in line.message
    assert "\x07" not in line.message
    assert line.message.endswith("channel live")


async def test_escape_sequences_in_a_handle_are_stripped() -> None:
    relay = _Relay([], fails=ValueError("bad"))
    (line,) = await _report(relay, users=("a\x1b[31m.b",)).results()
    assert "\x1b" not in line.message
