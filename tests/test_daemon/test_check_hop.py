"""CheckHop and Wakes, directly: the window, the token's life, the flag."""

from __future__ import annotations

import asyncio
import re

import pytest

from biff.channel_check_pending import CheckRefusedError
from biff.daemon.check_hop import CHECK_SECONDS, CheckHop, ForeignReplyError
from biff.daemon.wakes import Wakes


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _hop() -> tuple[CheckHop, _Clock]:
    clock = _Clock()
    return CheckHop(clock), clock


def test_the_first_request_makes_a_128_bit_hex_token() -> None:
    hop, _ = _hop()
    assert re.fullmatch(r"[0-9a-f]{32}", hop.request("_INBOX.abc"))


def test_a_second_request_inside_the_window_is_refused_and_does_not_extend_it() -> None:
    hop, clock = _hop()
    assert hop.request("_INBOX.one")
    clock.now += CHECK_SECONDS - 1
    with pytest.raises(CheckRefusedError, match=r"rate-limited \(60 s\)"):
        hop.request("_INBOX.two")
    clock.now += 1  # the window ran from the first request, not the refused one
    assert hop.request("_INBOX.three")


def test_a_foreign_subject_is_refused() -> None:
    hop, _ = _hop()
    with pytest.raises(ForeignReplyError, match="not an inbox"):
        hop.request("attacker.subject")


def test_the_right_token_returns_the_reply_once() -> None:
    hop, _ = _hop()
    token = hop.request("_INBOX.abc")
    assert hop.acknowledge(token) == "_INBOX.abc"
    assert hop.acknowledge(token) == ""


def test_a_wrong_or_non_ascii_token_changes_nothing() -> None:
    hop, _ = _hop()
    token = hop.request("_INBOX.abc")
    assert hop.acknowledge("0" * 32) == ""
    assert hop.acknowledge("é" * 16) == ""
    assert hop.acknowledge(token) == "_INBOX.abc"


def test_a_new_request_retires_the_older_token() -> None:
    hop, clock = _hop()
    old = hop.request("_INBOX.one")
    clock.now += CHECK_SECONDS
    new = hop.request("_INBOX.two")
    assert hop.acknowledge(old) == ""
    assert hop.acknowledge(new) == "_INBOX.two"


def test_nothing_is_acknowledged_when_nothing_is_pending() -> None:
    hop, _ = _hop()
    assert hop.acknowledge("") == ""
    assert hop.acknowledge("0" * 32) == ""


def test_a_listener_is_told_of_the_pending_token_first_and_then_of_new_ones() -> None:
    hop, clock = _hop()
    first = hop.request("_INBOX.one")
    with hop.listen() as tokens:
        assert tokens.get_nowait() == first
        clock.now += CHECK_SECONDS
        second = hop.request("_INBOX.two")
        assert tokens.get_nowait() == second


def test_a_listener_that_left_is_not_told() -> None:
    hop, _ = _hop()
    with hop.listen() as tokens:
        pass
    hop.request("_INBOX.one")
    assert tokens.empty()


def test_a_spent_token_is_not_offered_to_a_new_listener() -> None:
    hop, _ = _hop()
    hop.acknowledge(hop.request("_INBOX.one"))
    with hop.listen() as tokens:
        assert tokens.empty()


async def test_a_poke_sets_every_listeners_flag_and_a_repeat_changes_nothing() -> None:
    wakes = Wakes()
    with wakes.listen() as one, wakes.listen() as two:
        wakes.poke()
        wakes.poke()
        assert one.is_set() and two.is_set()
        one.clear()
        wakes.poke()
        assert one.is_set()


async def test_a_listener_that_left_keeps_its_flag_clear() -> None:
    wakes = Wakes()
    with wakes.listen() as flag:
        pass
    wakes.poke()
    assert not flag.is_set()
    assert isinstance(flag, asyncio.Event)


def test_a_restored_token_is_acknowledged_once_more() -> None:
    hop, _ = _hop()
    token = hop.request("_INBOX.one")
    assert hop.acknowledge(token) == "_INBOX.one"
    hop.restore(token, "_INBOX.one")
    assert hop.acknowledge(token) == "_INBOX.one"
    assert hop.acknowledge(token) == ""


def test_a_restore_does_not_replace_a_newer_token() -> None:
    hop, clock = _hop()
    old = hop.request("_INBOX.one")
    hop.acknowledge(old)
    clock.now += CHECK_SECONDS
    new = hop.request("_INBOX.two")
    hop.restore(old, "_INBOX.one")
    assert hop.acknowledge(old) == ""
    assert hop.acknowledge(new) == "_INBOX.two"
