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
import time
from collections import deque
from typing import TYPE_CHECKING, Self, final

from biff.nats_relay import NatsRelay
from biff.talk_types import (
    AcceptOutcome,
    AgentDrain,
    PendingInvite,
    QueuedNotification,
    TalkNotification,
    TalkPhase,
)

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

MAX_PENDING_INVITES = 100
"""Bound on the drained pending-invite set (notification.tex ``maxPending``).

The drop-oldest analog of ``MAX_TALK_QUEUE`` (DES-044) for ``talkPending``:
without it a peer forging invites from a stream of distinct sessions could
grow ``_pending`` without limit even as the queue stays bounded.  At the cap a
new inviter evicts the oldest-by-arrival entry; superseding an existing
inviter overwrites in place and never evicts.
"""

MAX_BODY_LEN = 512
"""Message body truncation limit (talk.tex ``maxBodyLen``)."""

PENDING_INVITE_TTL = 300.0
"""Seconds a pending talk invite survives unanswered (notification.tex
``maxInviteAge`` / ``ExpirePendingInvite``).

An invite whose inviter never returns and never sends an ``ntWithdraw``
would otherwise strand the ``[TALK]`` marker forever (biff-9la).  The poller
reaps invites older than this on its tick.  Five minutes comfortably exceeds
several talk poll cycles (~1 min each), so a genuinely-waiting invite is
never reaped before the agent can act, while a stranded marker still
self-heals within a bounded, human-scale window.  ``ntWithdraw`` is the fast
clean path; this TTL is the crash/disconnect backstop.
"""


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
    _pending: dict[str, PendingInvite]
    _queue: deque[QueuedNotification]

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
    def partner_tty(self) -> str:
        """The partner's tty, or our own when idle (sentinel).

        Names the connected partner's session so the connected hint reads as
        ``talk @user:tty`` (session-scoped, DES-043) rather than a bare
        ``@user`` that fails resolution when the partner has several sessions.
        """
        return self._partner_tty

    @property
    def partner_key(self) -> str:
        """The partner session key — the accept consent anchor (DES-043)."""
        return self._partner_key

    @property
    def partner_display(self) -> str:
        """The partner address as ``user:tty`` (or bare ``user`` when tty-less).

        The session-scoped form ``/who`` shows and ``talk @user:tty`` resolves,
        used when naming the current partner in a "already in a talk" refusal.
        """
        return (
            f"{self._partner}:{self._partner_tty}"
            if self._partner_tty
            else self._partner
        )

    @property
    def pending_invites(self) -> Mapping[str, PendingInvite]:
        """User-to-invite map of invites awaiting a response.

        Each value carries the inviter's session key (so the accept hint
        names a session) and monotonic arrival time (so the poller can age
        it out).
        """
        return dict(self._pending)

    @property
    def queued(self) -> int:
        """Number of notifications waiting to be drained."""
        return len(self._queue)

    @property
    def has_pending_traffic(self) -> bool:
        """Whether new inbound traffic is waiting to be drained right now.

        True when frames are queued or an invite already sits in
        ``pendingInvites`` (drained but unanswered).  ``talk_listen`` blocks on
        this so an invite that drained into ``pendingInvites`` with an empty
        queue returns at once, while a bare open connection with an empty queue
        keeps waiting for the partner's next frame — a live connection is not,
        by itself, new traffic to surface.
        """
        return bool(self._queue or self._pending)

    @property
    def queued_invite_users(self) -> tuple[str, ...]:
        """Distinct senders of undrained invite frames still in the queue.

        An invite rides the queue until ``talk_read`` drains it into
        ``pendingInvites``; before then the ``[TALK]`` marker must read as an
        invite ("wants to talk"), not a chat message, so a fresh unsolicited
        invite directs the agent to accept rather than to read a phantom
        message.  Order-preserving and de-duplicated so a repeated inviter is
        named once.
        """
        return tuple(
            dict.fromkeys(q.notif.nfrom for q in self._queue if q.notif.is_invite)
        )

    # -- Receive (talk.tex ReceiveNotification family) --

    def receive(self, raw: Mapping[str, object]) -> bool:
        """Enqueue a talk notification, applying the session-scope filters.

        Drops self-echo (``nfromKey == myKey``).  Every *modeled* talk frame
        (invite, accept, message, end, withdraw) is session-scoped: it is
        dropped unless ``nto == myKey`` (talk.tex ``ReceiveNotForSession``), so
        a forged or reordered frame with a foreign or empty ``nto`` — a typed
        ``message`` included — cannot apply to all of our sessions (DES-043).
        A wake poke — a mail/wall notification riding the talk subject with a
        missing or unrecognized ``type`` — is not a modeled talk frame: it is
        diverted *before* the session filter, wakes the poller (returns
        ``True``), but is *never* enqueued, so a ``/write`` mail body cannot
        surface as a phantom talk message.  On overflow, drops the oldest to
        retain the newest ``MAX_TALK_QUEUE`` (drop-oldest).  Returns ``True``
        when the frame should wake the poller (a real frame was enqueued, or a
        wake poke was accepted).
        """
        notif = TalkNotification.from_payload(raw)
        if notif.nfrom_key == self._my_key:
            return False  # ReceiveSelfEcho
        if notif.is_wake_poke:
            return True  # mail/wall poke — diverted before the session filter
        if notif.nto != self._my_key:
            return False  # ReceiveNotForSession — every modeled frame is scoped
        if notif.is_withdraw:
            return self._withdraw(notif.nfrom, notif.nfrom_key)  # WithdrawArrive
        if len(self._queue) >= MAX_TALK_QUEUE:
            self._queue.popleft()  # ReceiveOverflow — drop-oldest
        self._queue.append(QueuedNotification(notif, time.monotonic()))
        return True

    def _withdraw(self, inviter: str, withdraw_key: str) -> bool:
        """Cancel an invite only when the frame's key matches (WithdrawArrive).

        An ``ntWithdraw`` frame carries the inviter's originating session key.
        The invite — whether already drained into ``_pending`` or still queued
        and undrained — is cancelled only when that key equals the one recorded
        against it.  A frame whose key names a different session, or a user with
        no matching pending invite, is a foreign withdrawal and dropped with no
        state change (notification.tex WithdrawForeign): core NATS gives no
        cross-session ordering, so a late withdrawal from an earlier session
        must not disturb a live invite from a later one.  This is the
        withdrawal-side mirror of the accept consent guard (DES-043).  Returns
        ``True`` when something was removed, so the caller wakes the poller and
        re-derives the description.
        """
        pending = self._pending.get(inviter)
        matched = pending is not None and pending.session_key == withdraw_key
        if matched:
            del self._pending[inviter]
        before = len(self._queue)
        self._queue = deque(
            q
            for q in self._queue
            if not (
                q.notif.is_invite
                and q.notif.nfrom == inviter
                and q.notif.nfrom_key == withdraw_key
            )
        )
        return matched or len(self._queue) != before

    # -- Drain (talk.tex Drain* operations) --

    def drain_idle(self) -> list[TalkNotification]:
        """Drain the queue while idle; record invites in ``pendingInvites``.

        Mirrors talk.tex ``DrainInvite``: each invite maps its sender to
        the invite's originating session key so a later ``talk`` command
        can target its accept.  A newer invite supersedes the older one
        (function override).  Returns every drained notification for the
        front-end to render.
        """
        drained = self._drain_queued()
        for q in drained:
            if q.notif.is_invite:
                self._record_invite(q.notif, arrived=q.arrived)
        return [q.notif for q in drained]

    def _record_invite(
        self, notif: TalkNotification, *, arrived: float | None = None
    ) -> None:
        """Record an invite in the pending set (notification.tex TalkInviteArrive).

        Construction is the single validation gate: a frame whose session key
        does not name a session is rejected by :meth:`PendingInvite.from_notification`
        and dropped here with a debug log rather than recorded, since it could
        only render a bare ``talk @user`` hint that fails at the prompt
        (HintNamesSession).  The newest invite from a user supersedes the older.

        *arrived* carries the frame's original enqueue time so the TTL window is
        anchored to arrival, not to drain time — otherwise a late drain would
        restart the clock and let a stale invite outlive ``PENDING_INVITE_TTL``.

        The set is bounded at ``MAX_PENDING_INVITES``: a new inviter recorded at
        the cap evicts the oldest-by-arrival entry first (drop-oldest), while
        superseding an existing inviter overwrites in place and never evicts.
        """
        try:
            invite = PendingInvite.from_notification(notif, arrived=arrived)
        except ValueError:
            logger.debug("dropping malformed invite frame: %r", notif, exc_info=True)
            return
        at_capacity = len(self._pending) >= MAX_PENDING_INVITES
        if notif.nfrom not in self._pending and at_capacity:
            # Evict the true oldest by arrival time, not by dict insertion order:
            # superseding an inviter refreshes its ``arrived`` without moving its
            # key, so ``next(iter(...))`` could evict a recently-refreshed invite
            # (notification.tex TalkInviteArriveOverflow evicts oldest-by-arrival).
            oldest = min(self._pending, key=lambda u: self._pending[u].arrived)
            del self._pending[oldest]
        self._pending[notif.nfrom] = invite

    def poll_accept(self) -> tuple[AcceptOutcome, list[TalkNotification]]:
        """Drain the queue while inviting; detect accept or mutual auto-accept.

        Returns ``(outcome, banners)`` where *banners* are third-party
        invites the front-end should show while we wait.  Mirrors talk.tex
        ``DrainAcceptWhileInviting`` (accept from the invited session
        connects us), ``MutualAutoAccept`` (a mutual invite from the invited
        session, our key higher, auto-accepts), and ``DrainForeignAccept``
        (any other accept is discarded).  On a connecting outcome the phase
        advances to ``CONNECTED``.

        Message and end frames are *preserved* in the queue (not returned
        as banners): the accepter's opening line must render in the
        connected conversation once ``Connected`` prints, not as a phone
        banner during the handshake.
        """
        accepted = False
        auto = False
        banners: list[TalkNotification] = []
        keep: list[TalkNotification] = []
        for notif in self._drain():
            if notif.is_accept:
                if notif.nfrom_key == self._partner_key:
                    accepted = True  # DrainAcceptWhileInviting
                continue  # else DrainForeignAccept — discard
            if notif.is_invite:
                if notif.nfrom_key == self._partner_key:
                    if self._my_key > self._partner_key:
                        auto = True  # MutualAutoAccept (higher key)
                    continue  # lower key keeps waiting; no banner
                banners.append(notif)  # third-party invite → banner
                continue
            keep.append(notif)  # message / end — render after Connected
        now = time.monotonic()
        self._queue.extendleft(
            QueuedNotification(notif, now) for notif in reversed(keep)
        )
        if accepted or auto:
            self._phase = TalkPhase.CONNECTED
        if accepted:
            return AcceptOutcome.ACCEPTED, banners
        if auto:
            return AcceptOutcome.AUTO_ACCEPT, banners
        return AcceptOutcome.NONE, banners

    def drain_connected(self) -> tuple[list[TalkNotification], bool]:
        """Drain the queue while connected; surface messages and remote hangup.

        Mirrors talk.tex ``DrainMessage`` (a message is displayed) and
        ``DrainEnd`` (an end frame returns us to idle).  Invites and
        accepts are protocol noise here and dropped.  A message or end whose
        originating key is not the connected partner's is a foreign frame
        (talk.tex ``DrainForeignMessage`` / ``DrainForeignEnd``): it is
        dequeued and skipped so a forged frame cannot inject a line into the
        conversation or hang it up.  Returns ``(notifications, ended)`` where
        *ended* is ``True`` when the partner hung up; on hangup the state
        resets to idle.
        """
        surfaced: list[TalkNotification] = []
        ended = False
        for notif in self._drain():
            if notif.is_invite or notif.is_accept:
                continue
            if notif.nfrom_key != self._partner_key:
                continue  # DrainForeignMessage / DrainForeignEnd — not the partner
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
        (DrainAcceptWhileInviting / MutualAutoAccept), surfaces messages,
        and resets to idle on the connected partner's hangup (DrainEnd).
        An ``end`` frame drives a reset only in the connected phase, since
        talk.tex ``DrainEnd`` (and ``DrainForeignEnd``) guard
        ``phase = tpConnected``: a forged or stray end while inviting or idle
        is dequeued and dropped, never a reset, so it cannot cancel an
        outgoing invite.  While connected, a message or end from a key other
        than the partner's is a foreign frame (``DrainForeignMessage`` /
        ``DrainForeignEnd``), dequeued and skipped — never surfaced, never a
        hangup — so a forged frame cannot inject a line or tear down the
        conversation.  Returns an :class:`AgentDrain` snapshot; the ``talk``
        tool then decides whether to accept, connect, or invite.
        """
        messages: list[TalkNotification] = []
        ended = False
        auto_accept: TalkNotification | None = None
        for q in self._drain_queued():
            notif = q.notif
            if notif.is_invite:
                if self._absorb_invite(notif, arrived=q.arrived):
                    # Higher-key mutual glare: the caller must publish an accept
                    # so the lower-key partner connects (talk.tex MutualAutoAccept).
                    auto_accept = notif
            elif notif.is_accept:
                self._absorb_accept(notif)
            elif (
                self._phase is TalkPhase.CONNECTED
                and notif.nfrom_key != self._partner_key
            ):
                continue  # DrainForeignMessage / DrainForeignEnd — not the partner
            elif notif.is_end:
                if self._phase is TalkPhase.CONNECTED:
                    messages.append(notif)  # DrainEnd — the connected partner
                    ended = True
                # An end outside the connected phase is not a modeled reset
                # (talk.tex DrainEnd guards phase = tpConnected): drop it so a
                # forged end cannot cancel an outstanding outgoing invite.
            else:
                messages.append(notif)
        # Defer the reset until the whole batch drains: resetting mid-loop flips
        # phase CONNECTED→IDLE and disarms the foreign-frame guard for every
        # later frame, so a forged message trailing the partner's end would fall
        # through and surface.  drain_connected uses the same deferred-reset.
        if ended:
            self.reset()
        return AgentDrain(
            messages=tuple(messages),
            pending=dict(self._pending),
            auto_accept=auto_accept,
        )

    def _absorb_invite(self, notif: TalkNotification, *, arrived: float) -> bool:
        """Record an invite, or complete a mutual handshake by auto-accept.

        notification.tex TalkInviteArrive records the invite; when we are the
        higher-keyed party in a mutual invite it is instead consumed into a
        live connection (TalkAccept), so it is not left pending to strand the
        marker after hangup.  Returns ``True`` when this frame auto-accepted a
        mutual glare, so the caller can publish the obligatory accept frame
        (talk.tex MutualAutoAccept); ``False`` when the invite was merely
        recorded.
        """
        if (
            self._phase is TalkPhase.INVITING
            and notif.nfrom_key == self._partner_key
            and self._my_key > self._partner_key
        ):
            self._phase = TalkPhase.CONNECTED
            self._pending.pop(notif.nfrom, None)
            return True
        self._record_invite(notif, arrived=arrived)
        return False

    def _absorb_accept(self, notif: TalkNotification) -> None:
        """Complete our outstanding invite when the invited session accepts.

        notification.tex TalkAccept: activity moves from ``talkPending`` to
        ``talkConnected``; the accepted invite is consumed so it does not
        strand the marker.
        """
        if self._phase is TalkPhase.INVITING and notif.nfrom_key == self._partner_key:
            self._phase = TalkPhase.CONNECTED
            self._pending.pop(notif.nfrom, None)

    def expire_stale_invites(self, *, now: float | None = None) -> int:
        """Reap invites older than the TTL, drained or not; return the count.

        notification.tex ExpirePendingInvite: an invite whose monotonic age
        reaches ``PENDING_INVITE_TTL`` is removed on a poller tick.  The model
        holds every invite in ``talkPending`` from arrival, so the backstop must
        cover both the drained invites in ``_pending`` and an *undrained* invite
        still sitting in ``_queue`` — otherwise a never-drained invite (a
        crashed inviter that never sends ``ntWithdraw``, an idle-but-alive
        agent) strands the ``[TALK]`` marker forever, since the marker is lit by
        the queued frame.  A fresh invite (age below the bound) survives, the
        observable difference from ``ntWithdraw`` (immediate).  The caller
        re-derives the talk description after a non-zero return.
        """
        clock = time.monotonic() if now is None else now
        stale = [
            user
            for user, invite in self._pending.items()
            if clock - invite.arrived >= PENDING_INVITE_TTL
        ]
        for user in stale:
            del self._pending[user]
        return len(stale) + self._expire_queued_invites(clock)

    def _expire_queued_invites(self, clock: float) -> int:
        """Drop undrained invite frames past the TTL; return the number dropped.

        Only invite frames age out — a queued message clears by being drained
        (talk.tex ``DrainMessage``), never by time-to-live — so a stuck message
        is left untouched while a stranded invite is reaped.
        """
        before = len(self._queue)
        self._queue = deque(
            q
            for q in self._queue
            if not (q.notif.is_invite and clock - q.arrived >= PENDING_INVITE_TTL)
        )
        return before - len(self._queue)

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

    def consume_pending_invite(self, user: str) -> PendingInvite | None:
        """Pop and return the pending invite for *user*.

        One-shot: the pending invite is removed.  Returns ``None`` when no
        usable invite exists.  Keyless invites are never recorded, so a
        returned invite always names a session and carries the inviter's
        display tty — the caller sets ``partner_tty`` from it so the connected
        hint reads ``talk @user:ttyN``, never the opaque session-key hex.
        """
        return self._pending.pop(user, None)

    def restore_pending_invite(self, invite: PendingInvite) -> None:
        """Re-insert an invite consumed by a failed accept publish (CR-2).

        The accept path pops the invite *before* publishing the accept frame; a
        transient publish failure would otherwise discard it, so a retry would
        send a fresh *outbound* invite instead of re-accepting.  Restoring keeps
        the invite acceptable on the next attempt.  A newer invite from the same
        user that arrived meanwhile is not overwritten (``setdefault``), so the
        restore never clobbers a supersession.
        """
        self._pending.setdefault(invite.user, invite)

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
        await self._publish("message", target_user, to_key, body, target_repo)

    async def send_end(
        self,
        *,
        target_user: str,
        to_key: str,
        target_repo: str | None = None,
    ) -> None:
        """Publish a session-scoped end (hangup) frame."""
        await self._publish("end", target_user, to_key, "", target_repo)

    async def send_withdraw(
        self,
        *,
        target_user: str,
        to_key: str,
        target_repo: str | None = None,
    ) -> None:
        """Publish a session-scoped withdraw frame (ntWithdraw — cancel an invite).

        Sent when the inviter abandons an outstanding invite (talk_end while
        inviting).  The recipient drops the inviter's pending entry on receipt
        (notification.tex WithdrawArrive), so the marker reverts cleanly rather
        than waiting for the time-to-live sweep.
        """
        await self._publish("withdraw", target_user, to_key, "", target_repo)

    async def _publish(
        self,
        ntype: str,
        target_user: str,
        to_key: str,
        body: str,
        target_repo: str | None,
    ) -> None:
        """Publish one ephemeral talk frame; no-op for non-NATS relays.

        Every frame body is truncated to ``MAX_BODY_LEN`` here — the single
        DoS/footprint bound — so an oversized invite or accept body (both carry
        user input) cannot slip past a per-call-site slice and grow the peer's
        bounded queue to ``MAX_TALK_QUEUE`` frames of unbounded body.
        """
        relay = self._relay
        if not isinstance(relay, NatsRelay):
            return
        nc = await relay.get_nc()
        payload = json.dumps(
            {
                "type": ntype,
                "from": self._my_user,
                "from_tty": self._my_tty_name,
                "body": body[:MAX_BODY_LEN],
                "from_key": self._my_key,
                "to_key": to_key,
            }
        ).encode()
        subject = relay.talk_notify_subject(target_user, target_repo=target_repo)
        await nc.publish(subject, payload)

    def _drain_queued(self) -> list[QueuedNotification]:
        """Pop all queued notifications in FIFO order, keeping arrival stamps.

        Invite-recording drains (``drain_idle``, ``drain_for_agent``) need each
        frame's enqueue time to carry an invite's original TTL anchor into
        ``PendingInvite.arrived`` — so the pending window measures from arrival,
        not from drain.
        """
        drained = list(self._queue)
        self._queue.clear()
        return drained

    def _drain(self) -> list[TalkNotification]:
        """Pop all queued notifications in FIFO order, shedding arrival stamps.

        The enqueue-time stamp is a queue-only time-to-live anchor
        (:class:`QueuedNotification`); once drained, an invite's age is carried
        by its ``PendingInvite.arrived`` instead, so the frames surface bare.
        """
        return [q.notif for q in self._drain_queued()]
