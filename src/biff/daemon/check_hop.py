"""The channel check across the daemon: one token per request, one acknowledgement."""

from __future__ import annotations

import asyncio
import hmac
import re
import secrets
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, ClassVar, Self, final

from biff.channel_check_pending import CheckRefusedError

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

__all__ = ["CHECK_SECONDS", "CheckHop", "ForeignReplyError"]

CHECK_SECONDS = 60.0
"""A session accepts at most one check request in this many seconds."""


class ForeignReplyError(CheckRefusedError):
    """The reply is not an inbox subject: it is never published to."""


@final
class CheckHop:
    """Holds a session's one pending check and tells its listeners.

    ``docs/specs/channel-check.tex``.  ``Request``: a reply that is an inbox, at
    most one request a minute, a fresh 128-bit token that retires the older one.
    ``Acknowledge``: the pending token returns its reply subject once and is
    spent; a wrong or repeated token changes nothing.  Every listener is told of a
    new token, and a listener that starts while a token is pending is told of it
    too, so delivery is at least once and the client, which makes one channel
    event per token, absorbs a repeat.
    """

    _REPLY: ClassVar[re.Pattern[str]] = re.compile(r"_INBOX\.[A-Za-z0-9_.-]{1,120}")

    __slots__ = ("_clock", "_last_accepted", "_listeners", "_reply", "_token")

    _clock: Callable[[], float]
    _last_accepted: float  # minus infinity until the first request is accepted
    _token: str  # empty when no check is pending
    _reply: str
    _listeners: set[asyncio.Queue[str]]

    def __new__(cls, clock: Callable[[], float] = time.monotonic) -> Self:
        """Create a session's check state with nothing pending."""
        self = super().__new__(cls)
        self._clock = clock
        self._last_accepted = float("-inf")
        self._token = ""
        self._reply = ""
        self._listeners = set()
        return self

    def request(self, reply: str) -> str:
        """Accept a request naming *reply*; return its fresh token.

        Raises:
            ForeignReplyError: The reply is not an inbox subject.
            CheckRefusedError: A request was accepted within the window.  A
                refusal changes nothing and does not extend the window.
        """
        now = self._clock()
        if self._REPLY.fullmatch(reply) is None:
            msg = "the reply is not an inbox subject"
            raise ForeignReplyError(msg)
        if now < self._last_accepted + CHECK_SECONDS:
            msg = f"rate-limited ({CHECK_SECONDS:g} s)"
            raise CheckRefusedError(msg)
        self._last_accepted = now
        self._token = secrets.token_hex(16)
        self._reply = reply
        for queue in self._listeners:
            queue.put_nowait(self._token)
        return self._token

    def acknowledge(self, token: str) -> str:
        """Return the reply subject if *token* is the pending one, and spend it.

        A wrong, repeated or non-ASCII token returns ``""`` and changes nothing.
        """
        pending = self._token.encode()
        if not pending or not hmac.compare_digest(token.encode(), pending):
            return ""
        self._token = ""
        return self._reply

    def restore(self, token: str, reply: str) -> None:
        """Make *token* pending again, if no newer request replaced it.

        For an acknowledgement whose reply could not be sent, so the model's retry
        is accepted rather than refused as a repeat.
        """
        if not self._token:
            self._token = token
            self._reply = reply

    @contextmanager
    def listen(self) -> Generator[asyncio.Queue[str]]:
        """Yield a queue of the tokens made while the block runs, the pending first."""
        queue: asyncio.Queue[str] = asyncio.Queue()
        if self._token:
            queue.put_nowait(self._token)
        self._listeners.add(queue)
        try:
            yield queue
        finally:
            self._listeners.discard(queue)
