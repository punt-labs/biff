"""The channel check: one token per accepted request, one acknowledgement (DES-087).

``docs/specs/channel-check.tex`` models it.  ``biff doctor`` asks a session to prove
that channel events reach its model.  The server accepts at most one request a
minute, makes a fresh 128-bit token, sends it in one channel event, and accepts
the token back once through the ``channel_ack`` tool.  The check only reports; it
never changes what is delivered.
"""

from __future__ import annotations

import hmac
import logging
import re
import secrets
import time
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Self

from biff.server.channel_content import ChannelEvent

if TYPE_CHECKING:
    from biff.server.channel_reading import EventSink

logger = logging.getLogger(__name__)

CHECK_SECONDS = 60.0
"""A session accepts at most one check request in this many seconds."""

OWED_MEMORY = 8
"""How many owed tokens are remembered, so that a replayed request is not a new one."""


class ChannelCheck:
    """Hold the one pending check: its token, its reply subject, its send."""

    _REPLY = re.compile(r"_INBOX\.[A-Za-z0-9_.-]{1,120}")
    _clock: Callable[[], float]
    _last_accepted: float | None  # absent until the first request is accepted
    _token: str | None  # absent when no check is pending
    _reply: str
    _is_unsent: bool
    _owed: deque[str]  # tokens already owed, pending or spent, newest last

    def __new__(cls, clock: Callable[[], float] = time.monotonic) -> Self:
        """Create a session's check state with nothing pending."""
        self = super().__new__(cls)
        self._clock = clock
        self.reset()
        return self

    def reset(self) -> None:
        """Forget the cooldown and the pending token: the server serves a new session.

        A ``/clear`` or resume keeps the process but is a new session, which
        inherits neither the old session's cooldown nor its pending token.
        """
        self._last_accepted = None
        self._token = None
        self._reply = ""
        self._is_unsent = False
        self._owed = deque(maxlen=OWED_MEMORY)

    def request(self, reply: str) -> str | None:
        """Accept a request naming *reply*; return its fresh token, or ``None``.

        ``None`` means the request was refused: the reply is not an inbox, or a
        request was accepted less than a minute ago.  A refused request does not
        extend the window.  An accepted one retires any earlier token.
        """
        now = self._clock()
        if self._REPLY.fullmatch(reply) is None:  # a forger's subject
            return None
        if (
            self._last_accepted is not None
            and now < self._last_accepted + CHECK_SECONDS
        ):
            return None
        self._last_accepted = now
        self._token = secrets.token_hex(16)
        self._reply = reply
        self._is_unsent = True
        return self._token

    def owe(self, token: str) -> None:
        """Owe the event for *token*, made by biffd, which also took the request.

        biffd keeps the cooldown and the reply subject, so only the token and
        the fact that it is unsent are held here.  biffd delivers a request at
        least once, so a token already owed, whether its event is sent, pending
        or its acknowledgement spent, is not owed again.
        """
        if token in self._owed:
            return
        self._owed.append(token)
        self._token = token
        self._reply = ""
        self._is_unsent = True

    def take_unsent(self) -> str | None:
        """Return the token whose event is owed, once; ``None`` if none is owed."""
        if not self._is_unsent:
            return None
        self._is_unsent = False
        return self._token

    async def send_owed(self, link: EventSink) -> None:
        """Send the owed check event over *link*, once; it takes no rate slot.

        A check that cannot be sent is not retried: the doctor reports the
        session as not confirmed, which is true.  It never stops the arrivals
        the emitter announces after it.
        """
        token = self.take_unsent()
        if token is None:
            return
        event = ChannelEvent.channel_check(token)
        try:
            await link.send(event.content, event.meta)
        except Exception:
            logger.warning("channel check event not sent", exc_info=True)

    def acknowledge(self, token: str) -> str | None:
        """Return the reply subject if *token* is the pending one, and spend it.

        A wrong, repeated or non-ASCII token returns ``None`` and changes nothing.
        """
        pending = self._token
        if pending is None:
            return None
        if not hmac.compare_digest(token.encode(), pending.encode()):
            return None
        self._token = None
        return self._reply
