"""The channel check: one request a minute, one token, one acknowledgement (DES-087).

``docs/channel-check.tex`` models it.  The unit tests here name the transition
they prove (``Request``, ``RejectRequest``, ``Acknowledge``, ``RejectAck``).
"""

from __future__ import annotations

import re

import pytest

from biff.server.channel_check import CHECK_SECONDS, ChannelCheck, is_reply_subject
from biff.server.channel_content import ChannelEvent, CheckKind

_REPLY = "_INBOX.abc123"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def check(clock: _Clock) -> ChannelCheck:
    return ChannelCheck(clock)


class TestRequest:
    def test_the_first_request_makes_a_128_bit_hex_token(
        self, check: ChannelCheck
    ) -> None:
        """Request."""
        token = check.request(_REPLY)
        assert token is not None
        assert re.fullmatch(r"[0-9a-f]{32}", token)

    def test_a_second_request_inside_a_minute_is_refused(
        self, check: ChannelCheck, clock: _Clock
    ) -> None:
        """RejectRequest."""
        assert check.request(_REPLY) is not None
        clock.now += CHECK_SECONDS - 0.1
        assert check.request(_REPLY) is None

    def test_a_request_after_a_minute_is_accepted(
        self, check: ChannelCheck, clock: _Clock
    ) -> None:
        """Request."""
        assert check.request(_REPLY) is not None
        clock.now += CHECK_SECONDS
        assert check.request(_REPLY) is not None

    def test_a_refused_request_does_not_extend_the_window(
        self, check: ChannelCheck, clock: _Clock
    ) -> None:
        """RejectRequest."""
        check.request(_REPLY)
        clock.now += CHECK_SECONDS - 1
        assert check.request(_REPLY) is None
        clock.now += 1
        assert check.request(_REPLY) is not None

    def test_every_token_is_fresh(self, check: ChannelCheck, clock: _Clock) -> None:
        """Request."""
        first = check.request(_REPLY)
        clock.now += CHECK_SECONDS
        assert check.request(_REPLY) != first

    @pytest.mark.parametrize(
        "reply",
        ["", "talk.notify.kai:abc", "_INBOX.*", "_INBOX.>", "_INBOX.a b", "_INBOX."],
    )
    def test_a_reply_that_is_not_a_fresh_inbox_is_refused(
        self, check: ChannelCheck, reply: str
    ) -> None:
        """RejectRequest: the ack never goes to an arbitrary subject."""
        assert check.request(reply) is None

    def test_a_refused_reply_does_not_use_the_window(self, check: ChannelCheck) -> None:
        """RejectRequest."""
        assert check.request("biff.x.inbox.kai") is None
        assert check.request(_REPLY) is not None

    def test_the_reply_check_names_what_it_accepts(self) -> None:
        assert is_reply_subject("_INBOX.Zx9-q_1.k")
        assert not is_reply_subject("_INBOX." + "a" * 200)


class TestAcknowledge:
    def test_the_right_token_returns_the_reply_subject(
        self, check: ChannelCheck
    ) -> None:
        """Acknowledge."""
        token = check.request(_REPLY)
        assert token is not None
        assert check.acknowledge(token) == _REPLY

    def test_a_wrong_token_changes_nothing(self, check: ChannelCheck) -> None:
        """RejectAck."""
        token = check.request(_REPLY)
        assert token is not None
        assert check.acknowledge("0" * 32) is None
        assert check.acknowledge(token) == _REPLY

    def test_a_repeated_token_is_refused(self, check: ChannelCheck) -> None:
        """RejectAck."""
        token = check.request(_REPLY)
        assert token is not None
        check.acknowledge(token)
        assert check.acknowledge(token) is None

    def test_no_request_means_no_token_matches(self, check: ChannelCheck) -> None:
        """RejectAck."""
        assert check.acknowledge("") is None

    def test_a_newer_request_retires_the_older_token(
        self, check: ChannelCheck, clock: _Clock
    ) -> None:
        """Request replaces the pending token."""
        old = check.request(_REPLY)
        assert old is not None
        clock.now += CHECK_SECONDS
        new = check.request("_INBOX.second")
        assert new is not None
        assert check.acknowledge(old) is None
        assert check.acknowledge(new) == "_INBOX.second"

    def test_a_non_ascii_token_is_refused_not_raised(self, check: ChannelCheck) -> None:
        """RejectAck: hmac.compare_digest rejects non-ASCII str, so encode first."""
        check.request(_REPLY)
        assert check.acknowledge("é" * 32) is None


class TestTheEvent:
    def test_the_content_is_exactly_the_designed_sentence(self) -> None:
        event = ChannelEvent.channel_check("ab" * 16)
        assert event.content == (
            "biff channel check. Call channel_ack with token "
            f"{'ab' * 16}. Do not reply to the user."
        )

    def test_the_meta_kind_is_channel_check(self) -> None:
        assert ChannelEvent.channel_check("ab" * 16).meta == {"kind": "channel_check"}
        assert CheckKind.CHANNEL_CHECK.value == "channel_check"


class _Link:
    is_open = True

    def __init__(self, *, fails: bool) -> None:
        self.fails = fails
        self.sent: list[str] = []

    async def send(self, content: str, meta: object) -> None:
        if self.fails:
            raise TimeoutError
        self.sent.append(content)


class TestSendOwed:
    async def test_a_failed_send_is_not_raised_and_not_retried(
        self, check: ChannelCheck
    ) -> None:
        check.request(_REPLY)
        await check.send_owed(_Link(fails=True))

        link = _Link(fails=False)
        await check.send_owed(link)
        assert link.sent == []

    async def test_nothing_owed_sends_nothing(self, check: ChannelCheck) -> None:
        link = _Link(fails=False)
        await check.send_owed(link)
        assert link.sent == []

    def test_reset_forgets_the_cooldown_and_the_token(
        self, check: ChannelCheck
    ) -> None:
        token = check.request(_REPLY)
        assert token is not None
        check.reset()
        assert check.acknowledge(token) is None
        assert check.request(_REPLY) is not None
