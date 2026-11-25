"""The always-on subscription that receives channel check requests (DES-087).

One core-NATS subscription to this session's check subject.  A request wakes
the poller through the emitter; nothing is sent from the NATS callback itself,
because sending from a callback is unreliable.  The relay's other always-on
subscriptions are rebound on a new connection generation; this one is too.
A request lost while unbound is not replayed: the doctor's wait times out and
reports the session as not confirmed, which is true.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, Self

from nats.errors import Error as NatsError

from biff.channel_check_wire import CheckRequest, CheckWire
from biff.nats_relay import NatsRelay
from biff.talk_latch import LatchMessages, TalkNotifyLatch

if TYPE_CHECKING:
    from nats.aio.msg import Msg
    from nats.aio.subscription import Subscription

    from biff.server.state import ServerState

logger = logging.getLogger(__name__)

_MESSAGES = LatchMessages(
    onset="Channel check requests are down — subscribe failing, retrying each tick",
    retry="Channel check subscribe still failing, will retry",
    recovery="Channel check requests recovered — subscribed",
)


class ChannelCheckSub:
    """Bind, rebind and release the check-request subscription."""

    _state: ServerState
    _latch: TalkNotifyLatch
    _handle: Subscription | None
    _generation: int

    def __new__(cls, state: ServerState) -> Self:
        """Create the holder with nothing subscribed."""
        self = super().__new__(cls)
        self._state = state
        self._latch = TalkNotifyLatch(logger, _MESSAGES)
        self._handle = None
        self._generation = -1
        return self

    async def start(self) -> None:
        """Subscribe on the live client; a failure is logged and retried later."""
        relay = self._state.relay
        if not isinstance(relay, NatsRelay):
            return
        try:
            nc = await relay.get_nc()
            generation = relay.connection_generation
            handle = await CheckWire(nc).listen(
                relay.channel_check_subject(self._state.session_key), self._on_request
            )
        except (NatsError, OSError, TimeoutError):  # an outage: retried each tick
            self._latch.record_failure()
            return
        self._latch.record_success()
        self._handle, self._generation = handle, generation

    async def reconcile(self) -> None:
        """Rebind when unbound or when the relay dialed a new client."""
        relay = self._state.relay
        if not isinstance(relay, NatsRelay):
            return
        if self._handle is not None and self._generation >= relay.connection_generation:
            return
        await self._release()
        await self.start()

    async def close(self) -> None:
        """Unsubscribe, surviving a cancellation that arrives mid-way."""
        await self._release()

    async def _release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            with suppress(Exception, asyncio.CancelledError):
                await asyncio.shield(handle.unsubscribe())

    async def _on_request(self, msg: Msg) -> None:
        """Hand a well-formed request to the emitter; drop anything else."""
        try:
            request = CheckRequest.from_payload(msg.data)
        except ValueError:
            logger.debug("channel check request dropped: malformed", exc_info=True)
            return
        self._state.emitter.channel_check_requested(request.reply)
