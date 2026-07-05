"""Shared ephemeral talk state machine — one model, two front-ends.

Models ``docs/talk.tex``: the two-phase handshake, the bounded
notification queue, session-scoped delivery, and the mutual-invite
tie-break.  Both the CLI REPL and the MCP server compose a single
mutable :class:`TalkState` into their (frozen) session context, feed
every NATS talk notification into :meth:`TalkState.receive`, and drain
it in their own idiom — the REPL prints banners, the MCP server returns
the drained state through a tool and fires a tool-list-changed push.

Talk is ephemeral (BSD ``talk``): notifications ride NATS core pub/sub
with no durable inbox.  A dropped notification is simply lost, exactly
as a BSD talk invite evaporates when the inviter leaves.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Self, final

from biff.nats_relay import NatsRelay

if TYPE_CHECKING:
    from collections.abc import Mapping

    from biff.relay import Relay

logger = logging.getLogger(__name__)

MAX_TALK_QUEUE = 100
"""Bound on the notification queue (talk.tex ``maxQueueLen``, DES-044).

An unbounded queue is a flood/DoS vector: a peer can enqueue faster than
the 2s poll drains, growing memory without limit.  The queue is capped
with drop-oldest so the newest ``MAX_TALK_QUEUE`` notifications are
always retained (biff-vr4).
"""

MAX_BODY_LEN = 512
"""Message body truncation limit (talk.tex ``maxBodyLen``)."""


class TalkPhase(Enum):
    """The three phases of a talk session from one side's perspective."""

    IDLE = auto()  # tpIdle — normal mode, not in talk
    INVITING = auto()  # tpInviting — sent an invite, waiting for accept
    CONNECTED = auto()  # tpConnected — exchanging messages


class AcceptOutcome(Enum):
    """Result of polling for an accept while inviting (talk.tex handshake)."""

    NONE = auto()  # nothing yet — keep waiting
    ACCEPTED = auto()  # the partner accepted our invite
    AUTO_ACCEPT = auto()  # mutual invite; we are the higher key and auto-accept


@dataclass(frozen=True, slots=True)
class AgentDrain:
    """Result of an MCP agent-mode drain (non-modal front-end).

    The MCP server is not modal like the REPL, so it drains the whole
    queue in one pass: invites are recorded, an accept from the invited
    session connects us, messages are surfaced, and an end resets to idle.
    """

    messages: tuple[TalkNotification, ...]
    pending: Mapping[str, str]
    connected: bool
    ended: bool


@dataclass(frozen=True, slots=True)
class TalkNotification:
    """A typed talk notification (talk.tex ``Notification`` schema)."""

    ntype: str
    nfrom: str
    nfrom_tty: str
    nfrom_key: str
    nto: str
    nbody: str

    @classmethod
    def from_payload(cls, raw: Mapping[str, object]) -> Self:
        """Build a notification from a raw NATS JSON payload.

        Unknown ``type`` defaults to ``message`` — matching the REPL's
        historical drain behaviour where any non-control frame renders
        as a conversation line.
        """
        return cls(
            ntype=str(raw.get("type", "message")),
            nfrom=str(raw.get("from", "?")),
            nfrom_tty=str(raw.get("from_tty", "")),
            nfrom_key=str(raw.get("from_key", "")),
            nto=str(raw.get("to_key", "")),
            nbody=str(raw.get("body", "")),
        )

    @property
    def is_invite(self) -> bool:
        """Whether this is an ``invite`` control frame."""
        return self.ntype == "invite"

    @property
    def is_accept(self) -> bool:
        """Whether this is an ``accept`` control frame."""
        return self.ntype == "accept"

    @property
    def is_end(self) -> bool:
        """Whether this is an ``end`` control frame."""
        return self.ntype == "end"


@final
class TalkState:
    """Mutable ephemeral talk state for a single session (talk.tex ``TalkState``).

    Companion to the frozen ``CliContext``/``ServerState`` — holds the
    protocol state (phase, partner, pending invites) and the bounded
    notification queue that background NATS callbacks feed and the
    front-end poll drains.  Asyncio is single-threaded, so no locking
    is needed.
    """

    __slots__ = (
        "_my_key",
        "_my_tty",
        "_my_tty_name",
        "_my_user",
        "_partner",
        "_partner_key",
        "_partner_tty",
        "_pending",
        "_phase",
        "_queue",
        "_relay",
    )

    _relay: Relay
    _my_user: str
    _my_tty: str
    _my_tty_name: str
    _my_key: str
    _phase: TalkPhase
    _partner: str
    _partner_tty: str
    _partner_key: str
    _pending: dict[str, str]
    _queue: deque[TalkNotification]

    def __new__(
        cls,
        *,
        relay: Relay,
        user: str,
        tty: str,
        session_key: str,
        tty_name: str = "",
    ) -> Self:
        """Create an idle talk state anchored to *session_key* (talk.tex Init)."""
        self = super().__new__(cls)
        self._relay = relay
        self._my_user = user
        self._my_tty = tty
        self._my_tty_name = tty_name
        self._my_key = session_key
        self._phase = TalkPhase.IDLE
        self._partner = user
        self._partner_tty = tty
        self._partner_key = session_key
        self._pending = {}
        self._queue = deque()
        return self

    # -- Read-only state --

    @property
    def phase(self) -> TalkPhase:
        """The current talk phase."""
        return self._phase

    @property
    def partner(self) -> str:
        """The partner user, or our own user when idle (sentinel)."""
        return self._partner

    @property
    def partner_key(self) -> str:
        """The partner session key — the accept consent anchor (DES-043)."""
        return self._partner_key

    @property
    def pending_invites(self) -> Mapping[str, str]:
        """User to session-key map of invites awaiting a response."""
        return dict(self._pending)

    @property
    def queued(self) -> int:
        """Number of notifications waiting to be drained."""
        return len(self._queue)

    # -- Receive (talk.tex ReceiveNotification family) --

    def receive(self, raw: Mapping[str, object]) -> bool:
        """Enqueue a talk notification, applying the session-scope filters.

        Drops self-echo (``nfromKey == myKey``) and notifications
        addressed to another session (``nto`` set and not our key —
        DES-043).  On overflow, drops the oldest to retain the newest
        ``MAX_TALK_QUEUE`` (drop-oldest).  Returns ``True`` when the
        notification was enqueued.
        """
        notif = TalkNotification.from_payload(raw)
        if notif.nfrom_key == self._my_key:
            return False  # ReceiveSelfEcho
        if notif.nto and notif.nto != self._my_key:
            return False  # ReceiveNotForSession
        if len(self._queue) >= MAX_TALK_QUEUE:
            self._queue.popleft()  # ReceiveOverflow — drop-oldest
        self._queue.append(notif)
        return True

    # -- Drain (talk.tex Drain* operations) --

    def drain_idle(self) -> list[TalkNotification]:
        """Drain the queue while idle; record invites in ``pendingInvites``.

        Mirrors talk.tex ``DrainInvite``: each invite maps its sender to
        the invite's originating session key so a later ``talk`` command
        can target its accept.  A newer invite supersedes the older one
        (function override).  Returns every drained notification for the
        front-end to render.
        """
        drained = self._drain()
        for notif in drained:
            if notif.is_invite:
                self._pending[notif.nfrom] = notif.nfrom_key
        return drained

    def poll_accept(self) -> tuple[AcceptOutcome, list[TalkNotification]]:
        """Drain the queue while inviting; detect accept or mutual auto-accept.

        Returns ``(outcome, others)`` where *others* are third-party
        notifications the front-end should show as banners.  Mirrors
        talk.tex ``DrainAcceptWhileInviting`` (accept from the invited
        session connects us), ``MutualAutoAccept`` (a mutual invite from
        the invited session, our key higher, auto-accepts), and
        ``DrainForeignAccept`` (any other accept is discarded).  On a
        connecting outcome the phase advances to ``CONNECTED``.
        """
        accepted = False
        auto = False
        others: list[TalkNotification] = []
        for notif in self._drain():
            if notif.is_accept:
                if notif.nfrom_key == self._partner_key:
                    accepted = True  # DrainAcceptWhileInviting
                continue  # else DrainForeignAccept — discard
            if notif.is_invite and notif.nfrom_key == self._partner_key:
                if self._my_key > self._partner_key:
                    auto = True  # MutualAutoAccept (higher key)
                continue  # lower key keeps waiting; no banner
            others.append(notif)
        if accepted or auto:
            self._phase = TalkPhase.CONNECTED
        if accepted:
            return AcceptOutcome.ACCEPTED, others
        if auto:
            return AcceptOutcome.AUTO_ACCEPT, others
        return AcceptOutcome.NONE, others

    def drain_connected(self) -> tuple[list[TalkNotification], bool]:
        """Drain the queue while connected; surface messages and remote hangup.

        Mirrors talk.tex ``DrainMessage`` (a message is displayed) and
        ``DrainEnd`` (an end frame returns us to idle).  Invites and
        accepts are protocol noise here and dropped.  Returns
        ``(notifications, ended)`` where *ended* is ``True`` when the
        remote side hung up; on hangup the state resets to idle.
        """
        surfaced: list[TalkNotification] = []
        ended = False
        for notif in self._drain():
            if notif.is_invite or notif.is_accept:
                continue
            if notif.is_end:
                ended = True
            surfaced.append(notif)
        if ended:
            self.reset()
        return surfaced, ended

    def drain_for_agent(self) -> AgentDrain:
        """Drain the whole queue in one non-modal pass for the MCP agent.

        Records invites in ``pendingInvites`` (talk.tex DrainInvite), lets
        an accept from the invited session complete an outstanding invite
        (DrainAcceptWhileInviting / MutualAutoAccept), surfaces messages
        and end frames, and resets to idle on a remote hangup (DrainEnd).
        Returns an :class:`AgentDrain` snapshot; the ``talk`` tool then
        decides whether to accept, connect, or invite.
        """
        messages: list[TalkNotification] = []
        connected = False
        ended = False
        for notif in self._drain():
            if notif.is_invite:
                self._pending[notif.nfrom] = notif.nfrom_key
                if (
                    self._phase is TalkPhase.INVITING
                    and notif.nfrom_key == self._partner_key
                    and self._my_key > self._partner_key
                ):
                    self._phase = TalkPhase.CONNECTED
                    connected = True
            elif notif.is_accept:
                if (
                    self._phase is TalkPhase.INVITING
                    and notif.nfrom_key == self._partner_key
                ):
                    self._phase = TalkPhase.CONNECTED
                    connected = True
            elif notif.is_end:
                ended = True
                messages.append(notif)
                self.reset()
            else:
                messages.append(notif)
        return AgentDrain(
            messages=tuple(messages),
            pending=dict(self._pending),
            connected=connected,
            ended=ended,
        )

    # -- Local transitions (talk.tex Send*/Respond/End operations) --

    def begin_invite(self, *, partner: str, partner_tty: str, partner_key: str) -> None:
        """Enter the inviting phase for a specific session (talk.tex SendInvite)."""
        self._phase = TalkPhase.INVITING
        self._partner = partner
        self._partner_tty = partner_tty
        self._partner_key = partner_key

    def begin_connected(
        self, *, partner: str, partner_tty: str, partner_key: str
    ) -> None:
        """Enter the connected phase directly (talk.tex RespondToInvite)."""
        self._phase = TalkPhase.CONNECTED
        self._partner = partner
        self._partner_tty = partner_tty
        self._partner_key = partner_key

    def consume_pending_invite(self, user: str) -> str | None:
        """Pop and return the inviter's session key for *user*.

        One-shot: the pending invite is removed.  Returns ``None`` when
        no usable invite exists (absent, or an invite with no key).
        """
        return self._pending.pop(user, None) or None

    def reset(self) -> None:
        """Return to idle, clearing the partner sentinels (talk.tex LocalEnd)."""
        self._phase = TalkPhase.IDLE
        self._partner = self._my_user
        self._partner_tty = self._my_tty
        self._partner_key = self._my_key

    def set_tty_name(self, tty_name: str) -> None:
        """Update the display tty name used in outgoing notification frames."""
        self._my_tty_name = tty_name

    # -- Send (ephemeral core-NATS publish — talk.tex Send*/publish side effects) --

    async def send_invite(
        self,
        *,
        target_user: str,
        to_key: str,
        body: str = "",
        target_repo: str | None = None,
    ) -> None:
        """Publish a session-scoped invite frame."""
        await self._publish("invite", target_user, to_key, body, target_repo)

    async def send_accept(
        self,
        *,
        target_user: str,
        to_key: str,
        target_repo: str | None = None,
    ) -> None:
        """Publish a session-scoped accept frame."""
        await self._publish("accept", target_user, to_key, "", target_repo)

    async def send_message(
        self,
        *,
        target_user: str,
        to_key: str,
        body: str,
        target_repo: str | None = None,
    ) -> None:
        """Publish a session-scoped message frame (body truncated to the limit)."""
        await self._publish(
            "message", target_user, to_key, body[:MAX_BODY_LEN], target_repo
        )

    async def send_end(
        self,
        *,
        target_user: str,
        to_key: str,
        target_repo: str | None = None,
    ) -> None:
        """Publish a session-scoped end (hangup) frame."""
        await self._publish("end", target_user, to_key, "", target_repo)

    async def _publish(
        self,
        ntype: str,
        target_user: str,
        to_key: str,
        body: str,
        target_repo: str | None,
    ) -> None:
        """Publish one ephemeral talk frame; no-op for non-NATS relays."""
        relay = self._relay
        if not isinstance(relay, NatsRelay):
            return
        nc = await relay.get_nc()
        payload = json.dumps(
            {
                "type": ntype,
                "from": self._my_user,
                "from_tty": self._my_tty_name,
                "body": body,
                "from_key": self._my_key,
                "to_key": to_key,
            }
        ).encode()
        subject = relay.talk_notify_subject(target_user, target_repo=target_repo)
        await nc.publish(subject, payload)

    def _drain(self) -> list[TalkNotification]:
        """Pop all queued notifications in FIFO order."""
        drained = list(self._queue)
        self._queue.clear()
        return drained
