"""Read the wtmp session history stream without consuming it."""

from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from nats.js.api import ConsumerConfig, DeliverPolicy
from nats.js.errors import NotFoundError
from pydantic import ValidationError

from biff.models import SessionEvent

if TYPE_CHECKING:
    from nats.aio.msg import Msg
    from nats.js.client import JetStreamContext

logger = logging.getLogger(__name__)

FETCH_BATCH = 100
SCAN_BATCH = 1000  # history scans page through the whole retained window
FETCH_TIMEOUT = 1.0
CONSUMER_INACTIVE_THRESHOLD = 300.0  # 5 min — dead sessions auto-expire


@dataclass(frozen=True, slots=True)
class WtmpReader:
    """Reads wtmp events through a uniquely named consumer per read.

    Ephemeral consumers (``durable=None``) leak while the connection stays
    open, so each read creates a UUID-named consumer and deletes it after
    use; ``inactive_threshold`` is the safety net for crash cleanup.
    """

    js: JetStreamContext
    stream: str
    name_prefix: str

    async def recent(self, subject: str, count: int) -> list[SessionEvent]:
        """Return up to *count* events on *subject*, most recent first.

        Reads from near the stream tail, overshooting ``count * 2`` because
        the stream's last sequence counts every subject, not only *subject*.
        """
        try:
            info = await self.js.stream_info(self.stream)
        except NotFoundError:
            return []
        if (last_seq := info.state.last_seq) == 0:
            return []
        batch = max(count * 2, FETCH_BATCH)
        config = ConsumerConfig(
            deliver_policy=DeliverPolicy.BY_START_SEQUENCE,
            opt_start_seq=max(1, last_seq - batch + 1),
            inactive_threshold=CONSUMER_INACTIVE_THRESHOLD,
        )
        raws = await self._read(subject, config, batch=batch, drain=False)
        events = [e for e in map(self._parse, raws) if e is not None]
        events.sort(key=lambda e: e.timestamp, reverse=True)
        return events[:count]

    async def last_repo(self, subject: str, tty: str) -> str | None:
        """Return the repo of the newest event on *subject* for session *tty*.

        *tty* matches either the session's routing id or its name.  Stream
        order, not the event's own timestamp, decides which event is newest,
        so a back- or future-dated event cannot override a later one.  The
        repo is the subject's second token (``biff.{repo}.wtmp.{user}``).
        """
        config = ConsumerConfig(
            deliver_policy=DeliverPolicy.ALL,
            inactive_threshold=CONSUMER_INACTIVE_THRESHOLD,
        )
        repo: str | None = None
        for raw in await self._read(subject, config, batch=SCAN_BATCH, drain=True):
            event = self._parse(raw)
            if event is not None and tty in (event.tty, event.tty_name):
                repo = raw.subject.split(".")[1]
        return repo

    async def _read(
        self, subject: str, config: ConsumerConfig, *, batch: int, drain: bool
    ) -> list[Msg]:
        """Fetch one batch, or every pending message when *drain* is set."""
        name = f"{self.name_prefix}-{uuid4().hex[:12]}"
        sub = await self.js.pull_subscribe(
            subject, durable=name, stream=self.stream, config=config
        )
        raws: list[Msg] = []
        try:
            while True:
                try:
                    fetched = await sub.fetch(batch=batch, timeout=FETCH_TIMEOUT)
                except TimeoutError:
                    break
                raws.extend(fetched)
                if not drain or fetched[-1].metadata.num_pending == 0:
                    break
        finally:
            await sub.unsubscribe()
            with suppress(NotFoundError):
                await self.js.delete_consumer(self.stream, name)
        for raw in raws:
            await raw.ack()
        return raws

    @staticmethod
    def _parse(raw: Msg) -> SessionEvent | None:
        """Parse a wtmp message, or ``None`` when malformed or unsupported."""
        try:
            event = SessionEvent.model_validate_json(raw.data)
        except (ValidationError, ValueError):
            logger.warning("Skipping malformed wtmp message")
            return None
        if event.version != 1:
            logger.debug("Skipping wtmp v%d (unsupported)", event.version)
            return None
        return event
