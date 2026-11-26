"""A failed presence lookup is told apart from an empty one (doctor's check)."""

from __future__ import annotations

import pytest
from nats.js.errors import NotFoundError

from biff.models import UserSession
from biff.nats_relay import NatsRelay


class _Relay(NatsRelay):
    """A NATS relay that never dials; its repo lookup succeeds or fails on cue."""

    outcome: list[UserSession] | Exception

    async def _get_sessions_for_repo_inner(self, repo: str) -> list[UserSession]:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _relay(outcome: list[UserSession] | Exception) -> _Relay:
    relay = _Relay(url="nats://localhost:4222", repo_name="r")
    relay.outcome = outcome
    return relay


async def test_a_failed_lookup_is_raised_not_hidden() -> None:
    relay = _relay(TimeoutError())
    assert await relay.get_sessions_for_user("kai") == []  # the swallowed failure
    with pytest.raises(TimeoutError):
        await relay.list_sessions_for_user("kai")


async def test_the_sessions_of_one_user_are_listed() -> None:
    kai = UserSession(user="kai", tty="a")
    eric = UserSession(user="eric", tty="b")
    assert await _relay([kai, eric]).list_sessions_for_user("kai") == [kai]


async def test_a_bad_handle_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Invalid username"):
        await _relay([]).list_sessions_for_user("first.last")


async def test_a_repo_with_no_sessions_yet_is_empty_not_a_failure() -> None:
    assert await _relay(NotFoundError()).list_sessions_for_user("kai") == []
