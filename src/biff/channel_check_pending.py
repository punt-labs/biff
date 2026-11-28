"""A channel check in flight: the wait for the one session it was sent to."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, cast

from biff.channel_check_wire import CheckAck, CheckRequest

if TYPE_CHECKING:
    from nats.aio.subscription import Subscription

    from biff.channel_check_wire import CheckPort

__all__ = ["CheckLostError", "CheckRefusal", "CheckRefusedError", "PendingCheck"]

logger = logging.getLogger(__name__)


class CheckRefusedError(Exception):
    """The session's daemon refused the check; the message is the reason."""


class CheckLostError(ConnectionError):
    """The wait for the acknowledgement ended because the relay link was lost."""


@dataclass(frozen=True, slots=True)
class CheckRefusal:
    """The answer to a request the session's daemon will not act on, and why."""

    reason: str

    def to_payload(self) -> bytes:
        """Return the wire form."""
        return json.dumps({"refused": self.reason}).encode()

    @classmethod
    def of(cls, data: bytes) -> str:
        """Return the reason *data* refuses with, or ``""`` when it is no refusal."""
        try:
            raw: object = json.loads(data)
        except ValueError:
            return ""
        reason = (
            cast("dict[str, object]", raw).get("refused")
            if isinstance(raw, dict)
            else None
        )
        return reason if isinstance(reason, str) else ""


class PendingCheck:
    """A request in flight: wait for the one session it was sent to to answer."""

    _sub: Subscription
    _session_key: str

    def __new__(cls, sub: Subscription, session_key: str) -> Self:
        """Wait on *sub* for an acknowledgement from *session_key*."""
        self = super().__new__(cls)
        self._sub = sub
        self._session_key = session_key
        return self

    @classmethod
    async def start(cls, nc: CheckPort, subject: str, session_key: str) -> Self:
        """Send a request to *subject* with a fresh reply inbox; return the wait.

        The reply subscription is flushed to the server before the request goes
        out, so an acknowledgement cannot arrive before anyone is listening.
        """
        reply = nc.new_inbox()
        sub = await nc.subscribe(reply)
        try:
            await nc.flush()
            await nc.publish(subject, CheckRequest(reply).to_payload())
        except BaseException:
            # No PendingCheck exists yet, so nobody else can close the inbox.
            await sub.unsubscribe()
            raise
        return cls(sub, session_key)

    async def confirmed(self, timeout: float) -> bool:
        """Whether *session_key* acknowledged within *timeout* seconds.

        An acknowledgement from any other session is not the answer and the wait
        goes on until the deadline; one that does not parse is logged and skipped.

        Raises:
            CheckRefusedError: The session's daemon refused the request.
            CheckLostError: The reply subscription closed before any answer.
        """
        try:
            async with asyncio.timeout(timeout):
                async for msg in self._sub.messages:
                    if reason := CheckRefusal.of(msg.data):
                        raise CheckRefusedError(reason)
                    try:
                        ack = CheckAck.from_payload(msg.data)
                    except ValueError:
                        logger.warning("unreadable channel check reply ignored")
                        continue
                    if ack.session_key == self._session_key:
                        return True
        except TimeoutError:
            return False
        lost = "the relay connection closed while waiting for the acknowledgement"
        raise CheckLostError(lost)

    async def close(self) -> None:
        """Stop listening on the reply subject."""
        await self._sub.unsubscribe()
