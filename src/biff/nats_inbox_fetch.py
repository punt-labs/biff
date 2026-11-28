"""Consume an inbox subject of a NATS work queue without losing what it consumed."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, ClassVar

from nats.errors import Error as NatsError
from nats.js.api import ConsumerConfig
from pydantic import ValidationError

from biff.models import Message
from biff.nats_connection import Scrub
from biff.nats_session_rows import SessionRows
from biff.relay_errors import RELAY_ERRORS
from biff.wtmp_reader import CONSUMER_INACTIVE_THRESHOLD, FETCH_BATCH, FETCH_TIMEOUT

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from nats.aio.client import Client as NatsClient
    from nats.aio.msg import Msg
    from nats.js import JetStreamContext

__all__ = ["InboxFetch"]

logger = logging.getLogger(__name__)

_CONFIRM_TIMEOUT = 1.0  # one round trip to the server, all acks at once


class InboxFetch(SessionRows):
    """The consuming fetch of a NATS relay, which a cancelled caller cannot lose.

    A mixin of the relay: it needs the relay's stream, subject prefix, repo name,
    client and ``deliver``, declared below and supplied by the relay.

    A fetch removes mail from the work queue when the server confirms an ack.
    The caller that asked may be cancelled after that and before it holds the
    mail (a CLI deadline, a daemon stopping), so the fetch runs on to its end
    regardless, and mail whose caller left is filed again where it came from.
    """

    __slots__ = ()

    _orphans: ClassVar[set[asyncio.Task[None]]] = set()

    _subject_prefix: str
    _repo_name: str
    _nc: NatsClient | None

    async def deliver(
        self,
        message: Message,
        *,
        sender_key: str = "",
        target_repo: str | None = None,
    ) -> None:
        """Publish *message*; the relay supplies this."""
        raise NotImplementedError

    async def _fetch_from_subject(
        self, js: JetStreamContext, subject: str, durable: str, stream: str
    ) -> list[Message]:
        """Return the valid frames of *subject*, consumed.

        Shared by :meth:`fetch` (TTY inbox) and :meth:`fetch_user_inbox` (user
        broadcast inbox).  The fetch is shielded: a caller cancelled before it
        returns does not stop it, and the mail it consumed is filed again.
        """
        task = asyncio.ensure_future(self._consume(js, subject, durable, stream))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(self._file_again)
            raise

    def _file_again(self, done: asyncio.Future[list[Message]]) -> None:
        """File the mail of a fetch whose caller left, so the next fetch finds it."""
        if done.cancelled() or done.exception() is not None:
            return
        refile = asyncio.ensure_future(self._refile(done.result()))
        InboxFetch._orphans.add(refile)
        refile.add_done_callback(InboxFetch._orphans.discard)

    async def _refile(self, messages: list[Message]) -> None:
        for message in messages:
            try:
                await self.deliver(message.refiled())
            except RELAY_ERRORS:
                logger.exception("could not file message %s again", message.id)

    async def _consume(
        self, js: JetStreamContext, subject: str, durable: str, stream: str
    ) -> list[Message]:
        """Pull valid frames, ack them, and delete the consumer for a subject.

        A valid frame is acked (WORK_QUEUE deletes it); a malformed frame is
        terminated, never acked, so a wire-integrity fault is not silently
        destroyed as if delivered.  Every ack and termination is confirmed by
        the server before the consumer is deleted, all at once, so the whole
        batch costs one round trip.  A frame whose ack is not confirmed is still
        returned: it may be consumed or still queued, and a copy is the safe
        side of that.
        """
        sub = await js.pull_subscribe(
            subject,
            durable=durable,
            stream=stream,
            config=ConsumerConfig(inactive_threshold=CONSUMER_INACTIVE_THRESHOLD),
        )
        try:
            try:
                raw_msgs = await sub.fetch(batch=FETCH_BATCH, timeout=FETCH_TIMEOUT)
            except TimeoutError:
                raw_msgs = []
            finally:
                await sub.unsubscribe()

            valid: list[tuple[Msg, Message]] = []
            confirmations: list[Awaitable[object]] = []
            for raw in raw_msgs:
                try:
                    valid.append((raw, Message.model_validate_json(raw.data)))
                except (ValidationError, ValueError) as exc:
                    # A malformed frame must never be acked: ack removes it from
                    # the WORK_QUEUE as if delivered, silently destroying the
                    # evidence of a wire-integrity fault.  term() is JetStream's
                    # poison-message signal: it stops redelivery (no nak DoS
                    # loop on a persistently-bad frame) and emits a
                    # MSG_TERMINATED advisory so the drop is observable off-box.
                    #
                    # Never interpolate str(exc): ValidationError.__str__ embeds
                    # input_value == the raw frame body (private plaintext), and
                    # term() then deletes the only other copy.  Log field
                    # locations and error types only, with input/url/context
                    # scrubbed (PII guard).
                    logger.error(
                        "Terminating malformed NATS frame in %s inbox (%d bytes): %s",
                        ("user", "session")[
                            subject.removeprefix(self._subject_prefix + ".").count(".")
                        ],
                        len(raw.data),
                        Scrub.validation_error(exc),
                    )
                    confirmations.append(self._terminate(raw))
            confirmations += [raw.ack_sync() for raw, _ in valid]
            outcomes = await asyncio.gather(*confirmations, return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    logger.warning(
                        "%s: ack unconfirmed (%s)", self._repo_name, type(outcome)
                    )
            return [message for _, message in valid]
        finally:
            # Delete consumer after use — frees the server-side slot.
            # Broad except: any delete_consumer failure must not prevent
            # returning already-acked messages (WORK_QUEUE deletes on ack).
            # inactive_threshold provides backup cleanup.
            try:
                await js.delete_consumer(stream, durable)
            except (NatsError, TimeoutError):
                logger.debug("Consumer cleanup failed (will expire)")

    async def _terminate(self, raw: Msg) -> None:
        """Terminate *raw* and wait for the server to confirm it did.

        ``term()`` is a publish the server may not have processed when the
        consumer is deleted, which would make it redeliver the frame; a request
        to the ack subject is answered once it has.
        """
        if self._nc is None or raw.reply == "":
            msg = "cannot confirm the termination of a frame with no connection"
            raise NatsError(msg)
        await self._nc.request(raw.reply, b"+TERM", timeout=_CONFIRM_TIMEOUT)
