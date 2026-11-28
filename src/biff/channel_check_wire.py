"""What a channel check puts on the relay: the request, the reply, the wait (DES-087).

``biff doctor`` publishes a request naming a one-time reply subject to a
session's check subject and waits on the reply subject.  The session's server
publishes ``{session_key, token}`` there once its model acknowledges the check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, Self, cast

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from nats.aio.msg import Msg
    from nats.aio.subscription import Subscription


class _Payload:
    """A JSON object on the wire."""

    __slots__ = ()

    @staticmethod
    def fields(data: bytes, what: str) -> dict[str, object]:
        """Parse *data* as a JSON object; raise ``ValueError`` naming *what* if not."""
        try:
            raw: object = json.loads(data)
        except json.JSONDecodeError as exc:
            msg = f"channel check {what} is not JSON"
            raise ValueError(msg) from exc
        if not isinstance(raw, dict):
            msg = f"channel check {what} is not an object"
            raise ValueError(msg)
        return cast("dict[str, object]", raw)  # a JSON object's keys are always str


@dataclass(frozen=True, slots=True)
class CheckRequest(_Payload):
    """A doctor's request: the one-time subject the acknowledgement goes to."""

    reply: str

    def to_payload(self) -> bytes:
        """Return the wire form."""
        return json.dumps({"reply": self.reply}).encode()

    @classmethod
    def from_payload(cls, data: bytes) -> Self:
        """Parse a request; raise ``ValueError`` when it is not one."""
        reply = cls.fields(data, "request").get("reply")
        if not isinstance(reply, str):
            msg = "channel check request names no reply subject"
            raise ValueError(msg)
        return cls(reply)


@dataclass(frozen=True, slots=True)
class CheckAck(_Payload):
    """A session's acknowledgement: who answered and with which token."""

    session_key: str
    token: str

    def to_payload(self) -> bytes:
        """Return the wire form."""
        return json.dumps(
            {"session_key": self.session_key, "token": self.token}
        ).encode()

    @classmethod
    def from_payload(cls, data: bytes) -> Self:
        """Parse an acknowledgement; raise ``ValueError`` when it is not one."""
        fields = cls.fields(data, "acknowledgement")
        key, token = fields.get("session_key"), fields.get("token")
        if not isinstance(key, str) or not isinstance(token, str):
            msg = "channel check acknowledgement lacks session_key or token"
            raise ValueError(msg)
        return cls(key, token)


class CheckPort(Protocol):
    """The part of a NATS client the check uses, typed so no call is unknown.

    The client's own ``subscribe`` carries an unparameterised ``Future`` that
    type checkers report as partially unknown at every call site.
    """

    def new_inbox(self) -> str:
        """Return a fresh inbox subject."""
        ...

    async def subscribe(
        self,
        subject: str,
        queue: str = "",
        cb: Callable[[Msg], Awaitable[None]] | None = None,
    ) -> Subscription:
        """Subscribe to *subject*, calling *cb* for each message if given."""
        ...

    async def flush(self) -> None:
        """Wait until the server has processed everything sent so far."""
        ...

    async def publish(self, subject: str, payload: bytes) -> None:
        """Publish *payload* to *subject*."""
        ...


class CheckWire:
    """Publish and subscribe the check's messages on one NATS client."""

    _nc: CheckPort

    def __new__(cls, nc: CheckPort) -> Self:
        """Use *nc* for every message."""
        self = super().__new__(cls)
        self._nc = nc
        return self

    async def listen(
        self, subject: str, cb: Callable[[Msg], Awaitable[None]]
    ) -> Subscription:
        """Subscribe to *subject*, calling *cb* for each message."""
        return await self._nc.subscribe(subject, cb=cb)

    async def refuse(self, reply: str, payload: bytes) -> None:
        """Publish a refusal's *payload* to the doctor's *reply* subject."""
        await self._nc.publish(reply, payload)

    async def acknowledge(self, reply: str, ack: CheckAck) -> None:
        """Publish *ack* to the doctor's *reply* subject."""
        await self._nc.publish(reply, ack.to_payload())
