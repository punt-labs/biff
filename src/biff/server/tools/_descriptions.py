"""Dynamic tool description updates and the arrival poller.

Refreshes tool descriptions based on current server state.
Called after every tool execution (belt) and by a background
poller (suspenders) so notifications stay fresh even between
tool calls.  The poller runs only when something arrives: a
relay push, a local-relay file change, or the liveness reconcile.

After mutating the ``read_messages`` description, fires
``notifications/tools/list_changed`` so Claude Code re-reads
the tool list and sees the updated unread count.

Also writes an ``unread.json`` status file (when configured) so that
external tools like the Claude Code status bar can display a live
unread count without querying the MCP server.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, auto
from typing import TYPE_CHECKING, NamedTuple, Protocol

from mcp.shared.subscriptions import ToolsListChanged
from mcp.types import ToolListChangedNotification
from mcp.types.version import MODERN_PROTOCOL_VERSIONS

from biff.formatting import sanitized_sender, terminal_safe
from biff.models import UnreadSummary, WallPost
from biff.plugins.claude_code.unread_file import write_unread_file
from biff.server.display_queue import DisplayItem
from biff.talk_latch import LatchMessages, TalkNotifyLatch
from biff.talk_state import TalkState
from biff.talk_types import TalkNotification, TalkPhase
from biff.tty import format_address


class _WallLookup(Enum):
    """Distinguish fetching the wall from a known empty wall."""

    FETCH = auto()


if TYPE_CHECKING:
    from fastmcp import FastMCP
    from mcp.server.session import ServerSession
    from mcp.server.subscriptions import SubscriptionBus

    from biff.server.state import ServerState

logger = logging.getLogger(__name__)

_READ_MESSAGES_BASE = "Check your inbox for new messages. Marks all as read."

# The one timer the poller keeps: how often a NATS-backed poller checks that
# its always-on subscriptions are bound to the live client.  It is part of
# connection liveness (DES-041, DES-042, DES-087), not a cadence for finding
# arrivals — see ``WakeSignal.wait``.  A client replacement leaves every SUB
# orphaned until this check rebinds it, so it bounds that gap.
SUB_RECONCILE_INTERVAL = 5.0


# Wording for the inbox-notify always-on SUB's re-subscribe latch (DES-062),
# constructed directly against TalkNotifyLatch's public LatchMessages
# parameter — the class already generalizes over "what failed", only the
# text differs from the talk flavour (TalkNotifyLatch.for_resubscribe).
_INBOX_NOTIFY_RESUBSCRIBE_MESSAGES = LatchMessages(
    onset=(
        "Inbox-notify wake pokes are down — re-subscribe failing, retrying each tick"
    ),
    retry="Inbox-notify re-subscribe still failing, will retry",
    recovery="Inbox-notify wake pokes recovered — re-subscribed",
)

_WALL_NOTIFY_RESUBSCRIBE_MESSAGES = LatchMessages(
    onset=(
        "Wall-notify wake pokes are down — re-subscribe failing, retrying each tick"
    ),
    retry="Wall-notify re-subscribe still failing, will retry",
    recovery="Wall-notify wake pokes recovered — re-subscribed",
)

_NOTIFY_SEND_TIMEOUT = 3.0


async def _publish_tool_change(bus: SubscriptionBus) -> None:
    try:
        async with asyncio.timeout(_NOTIFY_SEND_TIMEOUT):
            await bus.publish(ToolsListChanged())
    except Exception:
        logger.warning("Tool list subscription publish failed", exc_info=True)


@dataclass
class _HandshakeDelivery:
    """Track legacy notification debt under one lock (DES-061)."""

    _session: ServerSession | None = None
    _pending_notify: bool = False
    # Hold across send and debt update so a concurrent success cannot erase a drop.
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def capture_session(self, session: ServerSession) -> None:
        """Capture the handshake session and flush any notification owed."""
        if session.protocol_version in MODERN_PROTOCOL_VERSIONS:
            return
        async with self._lock:
            self._session = session
            if self._pending_notify:
                await self._notify_standalone()

    async def notify(self, bus: SubscriptionBus | None = None) -> None:
        """Publish to modern subscribers and notify the handshake client.

        Args:
            bus: Server event bus, or None to send the handshake notification only.
        """
        if bus is not None:
            await _publish_tool_change(bus)

        from fastmcp.server.dependencies import get_context  # noqa: PLC0415

        try:
            ctx = get_context()
        except RuntimeError:
            ctx = None

        if ctx is None:
            async with self._lock:
                await self._notify_standalone()
        elif ctx.session.protocol_version not in MODERN_PROTOCOL_VERSIONS:
            async with self._lock:
                try:
                    async with asyncio.timeout(_NOTIFY_SEND_TIMEOUT):
                        await ctx.send_notification(ToolListChangedNotification())
                except Exception:
                    logger.warning("Tool list push failed in request", exc_info=True)
                    self._pending_notify = True
                else:
                    self._pending_notify = False
                self._session = ctx.session

    async def _notify_standalone(self) -> None:
        if self._session is None:
            self._pending_notify = True
            return

        try:
            async with asyncio.timeout(_NOTIFY_SEND_TIMEOUT):
                await self._session.send_tool_list_changed()
        except Exception:
            logger.warning("Could not send standalone tool list change", exc_info=True)
            self._session = None
            self._pending_notify = True
        else:
            self._pending_notify = False


handshake_delivery = _HandshakeDelivery()


# Set by the ``tty`` tool so the unread file includes the session name.
_tty_name: str = ""

# Set by the ``mesg`` tool so the unread file includes availability state.
_biff_enabled: bool = True

# Track the last wall content spoken via vox to avoid re-speaking on
# every refresh_wall tick (the description changes due to the countdown
# timer, but the wall content is the same).
_spoken_wall_key: tuple[str, str] = ("", "")


def _last_spoken_wall_key() -> tuple[str, str]:
    """Return the last wall content key that was spoken."""
    return _spoken_wall_key


def _set_last_spoken_wall_key(key: tuple[str, str]) -> None:
    """Update the last-spoken wall content key."""
    global _spoken_wall_key
    _spoken_wall_key = key


def get_tty_name() -> str:
    """Return the module-level TTY name."""
    return _tty_name


def set_tty_name(name: str) -> None:
    """Update the module-level TTY name for unread file writes."""
    global _tty_name
    _tty_name = name


def set_biff_enabled(*, enabled: bool) -> None:
    """Update the module-level biff_enabled flag for unread file writes."""
    global _biff_enabled
    _biff_enabled = enabled


def _reset_session() -> None:
    """Reset process globals for test isolation."""
    global handshake_delivery, _tty_name, _biff_enabled, _spoken_wall_key
    handshake_delivery = _HandshakeDelivery()
    _tty_name = ""
    _biff_enabled = True
    _spoken_wall_key = ("", "")


async def _sync_unread_file(
    state: ServerState,
    *,
    summary: UnreadSummary | None = None,
) -> None:
    """Write the unread status file with current display queue state.

    Serializes the full queue snapshot to ``display_items`` so the
    status bar can rotate through all items independently.  Called
    after any state change that might affect what the status bar shows.

    Pass *summary* to reuse an already-fetched :class:`UnreadSummary`
    and avoid a redundant relay round-trip; when omitted, this fetches
    the session's own summary.

    Best-effort: called from refresh_read_messages, refresh_wall, and
    refresh_talk, all of which run after their own primary tool
    operation has already succeeded — the status file this writes is an
    external convenience for the Claude Code status bar, not something
    any of those primary operations depend on. A relay hiccup fetching
    the summary or session for it must not crash the caller's
    already-succeeded result. Each relay call here needs its own guard,
    independently of the primary operation's error handling.
    """
    if state.unread_path is None:
        return
    try:
        if summary is None:
            summary = await state.relay.get_unread_summary(state.session_key)
        # Fetch plan from the relay session (lives in NATS KV, not display queue).
        plan = ""
        session = await state.relay.get_session(state.session_key)
        if session is not None:
            plan = session.plan
    except Exception:
        logger.debug("_sync_unread_file: relay call failed", exc_info=True)
        return
    # Rotation is computed here, when the queue is read, not by a timer.
    state.display_queue.advance_if_due()
    items = state.display_queue.snapshot()
    write_unread_file(
        state.unread_path,
        summary,
        repo_name=state.config.repo_name,
        user=state.config.user,
        tty_name=_tty_name,
        biff_enabled=_biff_enabled,
        display_items=items,
        plan=plan,
    )


async def refresh_read_messages(mcp: FastMCP[ServerState], state: ServerState) -> bool:
    """Update the ``read_messages`` tool description with unread count.

    When the user has unread messages, the description changes to show
    the count, e.g. ``"Check messages (2 unread). Marks all as read."``

    When the inbox is empty, the description reverts to the base text.

    After mutation, fires ``notifications/tools/list_changed`` so the
    client re-reads the tool list and sees the new description.

    If ``state.unread_path`` is set, also writes the unread summary to
    a JSON file for status bar consumption.

    Best-effort: this is a side-channel description update, called after
    the primary operation of many tools (read_messages, write, finger,
    mesg, plan, who, tty) has already succeeded. A relay hiccup here must
    never crash that already-succeeded primary result — the caller has
    real data to return; losing it to a failure in an unrelated status
    update would be its own instance of the silent-failure class this
    codebase works to avoid, just inverted (a successful read reported as
    a crash instead of a failure reported as a success).

    Returns whether the unread-count *fetch* succeeded — ``False`` only
    when the relay call itself raised (never for the belt-path tool
    callers, which uniformly ignore the return value). ``_active_tick``
    uses this to re-mark the poke gate on a transient failure: the gate's
    own clear-before-refresh already happened, and without this signal a
    dropped relay call would freeze the count until the next backstop
    tick instead of retrying on the very next one. ``tool is None`` is a
    precondition failure, not a fetch outcome — no relay call was ever
    attempted, so it reports success (there is nothing for a retry to fix).
    """
    tool = await mcp.get_tool("read_messages")
    if tool is None:
        return True
    try:
        combined = await state.relay.get_unread_summary(state.session_key)
    except Exception:
        logger.debug(
            "refresh_read_messages: unread summary fetch failed", exc_info=True
        )
        return False
    old_desc = tool.description
    if combined.count == 0:
        tool.description = _READ_MESSAGES_BASE
    else:
        tool.description = (
            f"Check messages ({combined.count} unread). Marks all as read."
        )
    if tool.description != old_desc:
        await handshake_delivery.notify(state.tool_events)
    await _sync_unread_file(state, summary=combined)
    return True


async def refresh_wall(
    mcp: FastMCP[ServerState],
    state: ServerState,
    *,
    wall: WallPost | _WallLookup | None = _WallLookup.FETCH,
) -> None:
    """Update the ``wall`` tool description and display queue.

    When a wall is active, the description shows the current banner
    and a ``DisplayItem`` is added to the rotation queue with a unique
    source key (``wall:{posted_at}``).  Old walls stay in the queue
    until they expire naturally — the queue accumulates walls so the
    status bar can rotate through them.

    When no wall is active (cleared or all expired), all wall items
    are removed from the queue.

    Pass *wall* to skip the relay fetch when the caller already has
    the current wall (e.g. :func:`poll_inbox`).
    """
    from biff.formatting import format_remaining  # noqa: PLC0415
    from biff.server.tools.wall import WALL_BASE_DESCRIPTION  # noqa: PLC0415

    tool = await mcp.get_tool("wall")
    if tool is None:
        return
    current = await state.relay.get_wall() if isinstance(wall, _WallLookup) else wall
    old_desc = tool.description
    queue = state.display_queue
    if current is None:
        tool.description = WALL_BASE_DESCRIPTION
        queue.remove_by_kind("wall")
    else:
        remaining = format_remaining(current.expires_at)
        # Wall text/sender are remote-controlled; strip terminal escapes and
        # cap the sender length before they reach the tool description and
        # status line. Neither ``from_user`` nor ``from_tty`` has
        # a ``max_length`` on the wire, so both are clipped together via
        # sanitized_sender — clipping ``from_user`` alone before
        # concatenating an unbounded ``from_tty`` would leave the composed
        # label just as unboundable.
        wall_text = terminal_safe(current.text)
        sender = sanitized_sender(current.from_user, current.from_tty)
        tool.description = (
            f"[WALL] {wall_text} — {sender}, "
            f"expires in {remaining}. "
            "Use wall(clear=True) to remove."
        )
        # Duration arithmetic: wall-clock difference → monotonic expiry.
        # Safe because we convert to a relative seconds delta, not an
        # absolute timestamp, before passing to the monotonic-based queue.
        seconds_remaining = (current.expires_at - datetime.now(UTC)).total_seconds()
        queue.add(
            DisplayItem(
                kind="wall",
                text=f"{sender}: {wall_text}",
                source_key=f"wall:{current.posted_at.isoformat()}",
                expires_at=queue.expires_from_now(max(0.0, seconds_remaining)),
            )
        )
    if tool.description != old_desc:
        await handshake_delivery.notify(state.tool_events)
        # Voice new walls via vox (L1 integration — no-op if vox absent).
        # Only speak when the wall CONTENT changes, not when the countdown
        # timer updates the description. The description changes every tick
        # because "expires in {remaining}" decreases, but the wall text is
        # the same. Track the last-spoken wall key to avoid re-speaking.
        if current is not None:
            wall_content_key = (current.text, current.posted_at.isoformat())
            if wall_content_key != _last_spoken_wall_key():
                _set_last_spoken_wall_key(wall_content_key)
                from biff.integration.vox import (  # noqa: PLC0415
                    speak_fire_and_forget,
                    vibes_from_text,
                )

                spoken = terminal_safe(current.text)
                # from_user has no max_length on the wire; sanitized_sender
                # bounds it before it reaches subprocess argv.
                speak_fire_and_forget(
                    f"Wall from {sanitized_sender(current.from_user)}: {spoken}",
                    vibe_tags=vibes_from_text(spoken),
                )
        else:
            _set_last_spoken_wall_key(("", ""))
    await _sync_unread_file(state)


TALK_BASE_DESCRIPTION = (
    "Start a real-time conversation with a teammate or agent. "
    "Incoming messages appear on the status bar automatically. "
    "Use /write to reply. Use talk_end to close."
)


def _talk_description(talk: TalkState) -> str:
    """Build the ``talk`` tool description from the held ephemeral state.

    Prompts the model to call ``talk_read`` when there is undrained
    activity (an invite the human sent, or queued messages), and reflects
    a connected conversation otherwise.  Reads only — never drains.

    An invite is rendered as "wants to talk" whether it has already drained
    into ``pendingInvites`` or is still queued and undrained — an unsolicited
    invite must direct the agent to accept, never surface as a chat message.
    """
    invites = talk.pending_invites
    if invites:
        ordered = sorted(invites.values(), key=lambda inv: inv.user)
        who = ", ".join(terminal_safe(inv.user) for inv in ordered)
        # Direct talk_read/accept, not talk_end: there is no active session to
        # close, and talk_end returns "No active talk session" and clears
        # nothing when idle with only pending invites.
        return (
            f"[TALK] {who} wants to talk — call talk_read to see it, then "
            f"{terminal_safe(ordered[0].accept_command)} to accept."
        )
    queued_inviters = talk.queued_invite_users
    if queued_inviters:
        who = ", ".join(terminal_safe(user) for user in queued_inviters)
        # An undrained invite: talk_read moves it into pendingInvites and prints
        # the runnable accept hint, so direct the agent there rather than
        # mislabelling it as a new message.
        return (
            f"[TALK] {who} wants to talk — call talk_read to see it, then "
            "talk to accept."
        )
    if talk.queued:
        # Reached only when no invite is queued, so every queued frame is a chat
        # message.  Direct talk_read, not talk_end: queued messages are drained
        # by talk_read; talk_end is a no-op with nothing to close here.
        noun = "message" if talk.queued == 1 else "messages"
        return f"[TALK] {talk.queued} new {noun} — call talk_read to see them."
    if talk.phase is TalkPhase.CONNECTED:
        partner = terminal_safe(talk.partner)
        tty = terminal_safe(talk.partner_tty)
        address = format_address(partner, tty or None)
        return (
            f"[TALK] connected to {partner} — talk {address} <message> "
            "to reply, talk_end to close."
        )
    return TALK_BASE_DESCRIPTION


def talk_signal(talk: TalkState) -> tuple[tuple[str, ...], int, str]:
    """Return a change key for the held talk state (pending, queued, phase).

    The pending element is each invite's session key, not a bare count, so a
    same-count churn — one inviter's session superseded by another's within a
    single poll window — still changes the key and forces a description
    refresh, never leaving a stale accept-hint teaser.
    """
    pending = tuple(sorted(inv.session_key for inv in talk.pending_invites.values()))
    return (pending, talk.queued, talk.phase.name)


async def refresh_talk(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Update the ``talk`` tool description from the held ``TalkState``.

    Reads the shared ephemeral state (never drains it — the model's
    ``talk_read`` tool is the drainer) and, when the summary changes,
    mutates the tool description and fires
    ``notifications/tools/list_changed`` so Claude Code re-reads the tool
    list and the model calls ``talk_read`` (DES-020/021).  Ephemeral:
    nothing is persisted; the unread status file is still written for the
    human status bar.
    """
    tool = await mcp.get_tool("talk")
    if tool is None:
        return
    old_desc = tool.description
    tool.description = _talk_description(state.talk)
    if tool.description != old_desc:
        await handshake_delivery.notify(state.tool_events)
    await _sync_unread_file(state)


class _Unsubscribable(Protocol):
    """Structural contract for an always-on SUB handle: it can be torn down.

    Everything :class:`SubscriptionBinding` actually does with ``handle``
    is call ``unsubscribe()`` on it (the reconcile helper, and
    ``poll_inbox``'s teardown) — a real ``nats.aio.subscription.Subscription``
    satisfies this structurally with no inheritance, so a ``Protocol`` names
    the contract precisely instead of leaving ``handle`` an untyped
    ``object`` with three separate ``# type: ignore[attr-defined]`` sites
    papering over the gap.
    """

    async def unsubscribe(self) -> None: ...


class SubscriptionBinding(NamedTuple):
    """A live always-on core-NATS SUB paired with the connection generation it binds to.

    Generalizes across every always-on SUB kind biff holds (talk,
    inbox-notify per DES-062) — the
    ``SubKind``-indexed ``subGen`` family in ``nats-relay.tex``.  Each such
    SUB lives on one ``nats.connect`` client.  A wedge teardown
    (``_force_reconnect``) or a give-up close (``_on_closed``) drops that
    client and the next dial builds a fresh one with no SUB — the held
    handle is orphaned on the closed client.  Pairing the handle with the
    ``connection_generation`` it bound to lets the poller detect that
    replacement and re-subscribe, one kind at a time, via
    :func:`_reconcile_always_on_sub`.
    """

    handle: _Unsubscribable
    generation: int


def _relay_generation(state: ServerState) -> int:
    """Return the relay's current per-dial generation, or 0 if not NATS-backed."""
    from biff.nats_relay import NatsRelay  # noqa: PLC0415

    relay = state.relay
    return relay.connection_generation if isinstance(relay, NatsRelay) else 0


def _relay_pushes_inbox_notify(state: ServerState) -> bool:
    """Whether *state*'s relay can ever mark the inbox poke gate.

    Only ``NatsRelay`` carries the always-on inbox-notify SUB (DES-062); a
    filesystem-backed ``LocalRelay`` has no push mechanism of any kind, so
    gating its unread recompute on a poke that will never arrive would
    silently degrade detection to the ``nap_interval`` backstop with no
    compensating benefit — ``LocalRelay.get_unread_summary`` is a cheap
    local read, not the JetStream ``stream_info`` round-trip DES-062 exists
    to save.
    """
    from biff.nats_relay import NatsRelay  # noqa: PLC0415

    return isinstance(state.relay, NatsRelay)


class _InboxPokeGate:
    """Poke-gated unread recompute (DES-062).

    Shared between the inbox-notify SUB callback and the talk-SUB
    callback's wake-poke branch — a targeted message's wake poke rides the
    talk subject, not a second inbox-notify publish, but it must mark this
    same gate or the gated tick never recomputes — both call :meth:`mark`
    and nothing else, never a relay call (DES-020/DES-021) — and
    ``_active_tick``, which calls :meth:`claim` once per tick.

    :meth:`claim` is true when a poke has arrived since the last
    recompute.  There is no timer backstop (DES-087): a poke lost while the
    connection was down is recovered by the reconnect catch-up, and an
    explicit read recomputes the count.  It clears the poke in the same
    synchronous step as the check, *before* the caller's own ``await`` on
    the actual refresh: a poke that arrives while that refresh is still in
    flight sets ``_poked`` again, which nothing after the clear can
    clobber, so it survives to the next tick instead of being silently
    absorbed into a refresh that started before it arrived. A refresh
    that then fails should call :meth:`mark` again to re-arm the retry
    for the next tick.

    Starts poked so the first tick always recomputes once, matching the
    old ``last_count = -1`` "force initial refresh" idiom it replaces.
    """

    __slots__ = ("_poked",)

    def __init__(self) -> None:
        self._poked = True

    def mark(self) -> None:
        """Record that a wake poke arrived (called from a NATS callback)."""
        self._poked = True

    @property
    def pending(self) -> bool:
        """Whether a recompute is owed that no wake has yet claimed."""
        return self._poked

    def claim(self) -> bool:
        """Atomically decide whether a recompute is owed, and claim it if so.

        Returns ``True`` (having already cleared the poke) when the caller
        should recompute now; ``False`` when there is nothing to do.
        """
        due = self._poked
        self._poked = False
        return due


async def _reconcile_always_on_sub(
    current: SubscriptionBinding | None,
    relay_generation: int,
    resubscribe: Callable[[], Awaitable[SubscriptionBinding | None]],
    *,
    state: ServerState,
    gate: _InboxPokeGate,
) -> SubscriptionBinding | None:
    """Re-establish an always-on SUB when unbound or its client was replaced.

    Shared by every always-on-SUB kind (``nats-relay.tex`` ``Subscribe``,
    quantified over ``SubKind``): re-subscribes when there is no live SUB
    (initial-failure retry — NATS was down at startup) or when the relay
    dialed a new client past the generation the current SUB is bound to. The
    generation comparison — not a ``sub is None`` liveness probe — is the
    discriminator the proven model requires: a wedge teardown orphans the
    SUB on the closed client while leaving the handle object non-``None``,
    so an is-None test never fires and the SUB dies silently, for any kind.
    An in-place nats-py reconnect keeps the same generation and replays
    every SUB, so it is left untouched. The new generation is bound only on
    a successful re-subscribe.

    Invariant this enforces (every kind, uniformly): every event that can
    change unread state either marks the poke gate directly or is covered
    by the reconnect catch-up (DES-087). A newly (re)established SUB
    of ANY kind — first bind, a retry after an earlier failure, or a
    wedge-teardown re-subscribe — cannot see a poke published while nothing
    was listening; core NATS has no replay. The mark+wake lives here,
    once, so every caller inherits it rather than needing its own copy.
    """
    if current is not None and current.generation >= relay_generation:
        return current
    if current is not None:
        # The superseding dial already closed the old client; unsubscribing the
        # orphaned handle is best-effort and its failure is expected.
        with suppress(Exception):
            await current.handle.unsubscribe()
    fresh = await resubscribe()
    if fresh is not None:
        gate.mark()
        state.wake.wake()
    return fresh


async def subscribe_talk(
    state: ServerState,
    latch: TalkNotifyLatch,
    gate: _InboxPokeGate,
) -> SubscriptionBinding | None:
    """Establish the always-on talk subscription feeding the held ``TalkState``.

    Started once at poller start (ungated — a fresh agent must receive an
    unsolicited invite).  Every talk frame flows into
    ``state.talk.receive`` (self-echo and session-scope filtering happen
    there); when the frame wakes the poller, ``state.wake`` runs its next
    pass at once, so push latency is bounded by network and processing
    time alone.

    A wake poke — a ``/write`` message notification riding this subject
    with no recognized ``type`` (``TalkNotification.is_wake_poke``,
    ``talk_state.py`` diverts it before the session filter) — also marks
    *gate*.  Without this, a targeted message's only signal was
    ``state.wake.wake()``, which runs the next pass but has no say
    over whether the now poke-gated tick actually recomputes the unread
    count — the gate decides that. A genuine talk frame
    (invite/message/end/withdraw) does *not* mark the gate — marking it
    for talk activity unrelated to the inbox would be a spurious unread
    recompute.

    Captures ``connection_generation`` at the point of subscribe so a later
    client replacement is detectable: the returned pair binds the SUB to the
    generation of the client it actually runs on.
    """
    from biff.nats_relay import NatsRelay  # noqa: PLC0415

    if not isinstance(state.relay, NatsRelay):
        return None
    try:
        nc = await state.relay.get_nc()
        generation = state.relay.connection_generation
        subject = state.relay.talk_notify_subject(state.session_key)

        async def _on_talk_msg(msg: object) -> None:
            try:
                data = getattr(msg, "data", b"")
                raw: object = json.loads(data)
                if isinstance(raw, dict):
                    # Narrow only the keys to str (a JSON object's keys always
                    # are) — never the values. TalkNotification.from_payload's
                    # _trusted() type-guards each value itself, trusting only
                    # a str and falling back to the field's default for any
                    # other JSON type (None, number, dict, list). Converting
                    # values with str() here ran that guard *after* every
                    # value was already a str, defeating it: a forged
                    # ``{"body": null}`` became ``str(None)`` == ``"None"``,
                    # which passed the guard and rendered as a real message.
                    frame: dict[str, object] = {
                        str(k): v  # pyright: ignore[reportUnknownArgumentType]
                        for k, v in raw.items()  # pyright: ignore[reportUnknownVariableType,reportUnknownMemberType]
                    }
                    if TalkNotification.from_payload(frame).is_wake_poke:
                        gate.mark()  # a targeted message's wake poke, not a talk frame
                    woke = state.talk.receive(frame)
                else:
                    # A non-dict payload (a legacy ``b"1"`` bare wake) carries
                    # no frame — it cannot be a modeled talk frame either, so
                    # it is a wake poke by the same reasoning.
                    gate.mark()
                    woke = True
                if woke:
                    # Do NOT fire notifications here — sending from a NATS
                    # callback is unreliable (different coroutine context).
                    # Wake the poller; its next tick refreshes + notifies.
                    state.wake.wake()
            except (json.JSONDecodeError, AttributeError, TypeError):
                logger.debug("Failed to process talk notification", exc_info=True)

        handle = await nc.subscribe(  # pyright: ignore[reportUnknownMemberType]
            subject, cb=_on_talk_msg
        )
    except Exception:  # noqa: BLE001
        latch.record_failure()
        return None
    latch.record_success()
    return SubscriptionBinding(handle, generation)


async def _reconcile_talk_sub(
    state: ServerState,
    talk_sub: SubscriptionBinding | None,
    latch: TalkNotifyLatch,
    gate: _InboxPokeGate,
) -> SubscriptionBinding | None:
    """Re-establish the talk SUB when it is unbound or its client was replaced."""
    return await _reconcile_always_on_sub(
        talk_sub,
        _relay_generation(state),
        lambda: subscribe_talk(state, latch, gate),
        state=state,
        gate=gate,
    )


async def subscribe_inbox_notify(
    state: ServerState,
    latch: TalkNotifyLatch,
    gate: _InboxPokeGate,
) -> SubscriptionBinding | None:
    """Establish an always-on broadcast inbox-notify subscription (DES-062).

    Started once at poller start (ungated), mirroring :func:`subscribe_talk`
    exactly (``nats-relay.tex`` ``Subscribe``, kind ``inboxNotify``): the poke
    carries no payload, so the callback does exactly two things — marks
    *gate* so ``_active_tick`` knows a recompute is owed, and wakes the
    poller so the recompute runs at once — never a refresh or a notify
    directly (DES-020/DES-021). NATS-only.

    Binds to the broadcast inbox of ``state.config.user``.
    """
    from biff.nats_relay import NatsRelay  # noqa: PLC0415

    if not isinstance(state.relay, NatsRelay):
        return None
    try:
        nc = await state.relay.get_nc()
        generation = state.relay.connection_generation
        subject = state.relay.inbox_notify_subject(
            state.config.repo_name, state.config.user
        )

        async def _on_inbox_notify_msg(_msg: object) -> None:
            gate.mark()
            # Do NOT fire notifications here — sending from a NATS callback
            # is unreliable (different coroutine context). Wake the poller;
            # its next tick recomputes the unread count and notifies.
            state.wake.wake()

        handle = await nc.subscribe(  # pyright: ignore[reportUnknownMemberType]
            subject, cb=_on_inbox_notify_msg
        )
    except Exception:  # noqa: BLE001
        latch.record_failure()
        return None
    latch.record_success()
    return SubscriptionBinding(handle, generation)


async def _reconcile_inbox_notify_sub(
    state: ServerState,
    inbox_notify_sub: SubscriptionBinding | None,
    latch: TalkNotifyLatch,
    gate: _InboxPokeGate,
) -> SubscriptionBinding | None:
    """Re-establish an inbox-notify SUB when unbound or its client was replaced."""
    return await _reconcile_always_on_sub(
        inbox_notify_sub,
        _relay_generation(state),
        lambda: subscribe_inbox_notify(state, latch, gate),
        state=state,
        gate=gate,
    )


async def subscribe_wall_notify(
    state: ServerState,
    latch: TalkNotifyLatch,
) -> SubscriptionBinding | None:
    """Establish an always-on wall-change notify subscription.

    Started once at poller start (ungated), mirroring
    :func:`subscribe_inbox_notify`.  The poke is repo-scoped and
    user-independent — a wall is a team broadcast — so this binds to
    ``wall_notify_subject(repo)``, the one subject every session in the
    repo shares.  The payload-less poke's callback only wakes the poller.
    It never refreshes or notifies directly — sending from a NATS callback
    is unreliable (DES-020/DES-021).  The woken tick re-reads
    ``get_wall()`` (unconditional, every tick) and reconciles the display
    queue, which is what clears a stale wall on a peer.

    No gate mark: the inbox poke gate governs the unread-count recompute
    only; the wall read is already unconditional per tick, so waking the
    tick is sufficient.  NATS-only.
    """
    from biff.nats_relay import NatsRelay  # noqa: PLC0415

    if not isinstance(state.relay, NatsRelay):
        return None
    try:
        nc = await state.relay.get_nc()
        generation = state.relay.connection_generation
        subject = state.relay.wall_notify_subject(state.config.repo_name)

        async def _on_wall_notify_msg(_msg: object) -> None:
            state.wake.wake()

        handle = await nc.subscribe(  # pyright: ignore[reportUnknownMemberType]
            subject, cb=_on_wall_notify_msg
        )
    except Exception:  # noqa: BLE001
        latch.record_failure()
        return None
    latch.record_success()
    return SubscriptionBinding(handle, generation)


async def _reconcile_wall_notify_sub(
    state: ServerState,
    wall_notify_sub: SubscriptionBinding | None,
    latch: TalkNotifyLatch,
    gate: _InboxPokeGate,
) -> SubscriptionBinding | None:
    """Re-establish a wall-notify SUB when unbound or its client was replaced."""
    return await _reconcile_always_on_sub(
        wall_notify_sub,
        _relay_generation(state),
        lambda: subscribe_wall_notify(state, latch),
        state=state,
        gate=gate,
    )


@dataclass(frozen=True, slots=True)
class _Seen:
    """What the last pass saw, so the next pass refreshes only on a change.

    The defaults make the first pass refresh everything.
    """

    unread: int = -1  # the local relay's count comparison only
    wall: tuple[str, str] = ("", "")
    talk: tuple[tuple[str, ...], int, str] = ((), -1, "")


async def _active_tick(
    mcp: FastMCP[ServerState],
    state: ServerState,
    seen: _Seen,
    *,
    gate: _InboxPokeGate,
) -> _Seen:
    """One poller pass: check inbox, wall, and talk changes.

    Runs because something woke the poller, never on a schedule.

    Unread recompute is poke-gated (DES-062) for a NATS-backed relay, not
    unconditional: broadcast and targeted messages alike push a wake poke,
    so ``get_unread_summary`` only needs to run when *gate* says a poke
    arrived — ``refresh_read_messages``
    does its own fetch and its own change-gated notify, so there is
    nothing left for this tick to fetch redundantly. A relay with no push
    mechanism (``LocalRelay``) is woken by its file watch and compares the
    count against *last_count* — see :func:`_relay_pushes_inbox_notify`.

    Returns what this pass saw.
    """
    last_count, last_wall, last_talk = seen.unread, seen.wall, seen.talk
    if _relay_pushes_inbox_notify(state):
        if gate.claim():
            ok = await refresh_read_messages(mcp, state)
            if not ok:
                # The gate already cleared its poke before this await
                # started (claim()'s clear-before-refresh ordering). A
                # transient relay failure here must not freeze the count
                # until the next arrival — re-arm so the next pass retries.
                # ``poll_inbox`` runs that pass on its liveness wake.
                gate.mark()
    else:
        total = (await state.relay.get_unread_summary(state.session_key)).count
        if total != last_count:
            last_count = total
            await refresh_read_messages(mcp, state)

    # Check wall — key on (text, posted_at) so re-posts trigger refresh.
    current_wall = await state.relay.get_wall()
    wall_key = (
        (current_wall.text, current_wall.posted_at.isoformat())
        if current_wall
        else ("", "")
    )
    if wall_key != last_wall:
        last_wall = wall_key
        await refresh_wall(mcp, state, wall=current_wall)

    # Refresh talk tool description when the held TalkState changes
    # (new invite, queued message, phase transition).  Reading the state
    # ages out a stale invite (``TalkState.expire_stale_invites``).
    talk_key = talk_signal(state.talk)
    if talk_key != last_talk:
        last_talk = talk_key
        await refresh_talk(mcp, state)

    return _Seen(unread=last_count, wall=last_wall, talk=last_talk)


async def _safe_tick(
    mcp: FastMCP[ServerState],
    state: ServerState,
    seen: _Seen,
    *,
    gate: _InboxPokeGate,
) -> _Seen:
    """Wrap ``_active_tick`` with error handling.

    A transient NATS error must not kill the poller — log and retry
    on the next wake.  ``CancelledError`` is re-raised so shutdown
    propagates correctly.

    Logged at DEBUG, not WARNING: a NATS wedge would otherwise spam a
    warning per wake.  The relay's
    ``_ConnectionHealth`` is the single source of onset/recovery
    warnings — it logs the wedge once, not once per tick.
    """
    try:
        return await _active_tick(mcp, state, seen, gate=gate)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("Poller tick failed, will retry", exc_info=True)
        return seen


async def poll_inbox(
    mcp: FastMCP[ServerState],
    state: ServerState,
    *,
    shutdown: asyncio.Event | None = None,
) -> None:
    """Background task: react to arrivals, refresh notifications on change.

    Runs for the lifetime of the MCP server and does work only when woken:
    by a NATS push (talk frame, inbox poke, wall poke), by a KV watcher,
    or — for the local relay — by a file change in its data directory
    (:meth:`~biff.relay.LocalRelay.watch`).  Nothing schedules a pass
    (DES-087): the wall's remaining time, a talk invite's age and the unread
    count are computed when read, and an explicit ``read_messages`` call
    recomputes the count.

    The one timer is connection liveness.  A NATS-backed poller wakes every
    ``SUB_RECONCILE_INTERVAL`` to rebind its always-on subscriptions to the
    live client, and to retry an unread refresh that failed; it runs no
    other work then.  A filesystem relay has no subscriptions and no timer.

    Every wake interrupts the sleep immediately via ``state.wake``, so
    push-to-refresh latency is bounded by actual network/processing time.
    The first pass runs at start, to catch anything that arrived before the
    subscriptions and the watch were in place (core NATS does not replay).

    When *shutdown* is set, exits cleanly between iterations —
    no NATS operations are interrupted mid-flight.

    Establishes three always-on NATS subscriptions: talk notifications
    (ungated — a fresh agent must receive an unsolicited invite), the
    broadcast inbox-notify wake poke for this session's own user, and the
    wall poke. Every talk frame on this session's own subject flows into
    the shared ``TalkState``; a wake poke (message notification riding the
    talk subject) additionally marks the poke gate so a targeted message's
    detection is not silently deferred.
    """
    seen = _Seen()
    gate = _InboxPokeGate()
    liveness_timeout = (
        SUB_RECONCILE_INTERVAL if _relay_pushes_inbox_notify(state) else None
    )
    talk_latch = TalkNotifyLatch.for_resubscribe(logger)
    inbox_notify_latch = TalkNotifyLatch(logger, _INBOX_NOTIFY_RESUBSCRIBE_MESSAGES)
    wall_notify_latch = TalkNotifyLatch(logger, _WALL_NOTIFY_RESUBSCRIBE_MESSAGES)
    talk_sub: SubscriptionBinding | None = None
    inbox_notify_sub: SubscriptionBinding | None = None
    wall_notify_sub: SubscriptionBinding | None = None

    with state.wake.watching(state.relay):
        try:
            # Bind each subscription inside the cleanup scope so cancellation
            # during the next await cannot leak an established subscription.
            talk_sub = await subscribe_talk(state, talk_latch, gate)
            inbox_notify_sub = await subscribe_inbox_notify(
                state, inbox_notify_latch, gate
            )
            wall_notify_sub = await subscribe_wall_notify(state, wall_notify_latch)
            state.wake.wake()  # the first pass
            while shutdown is None or not shutdown.is_set():
                outcome = await state.wake.wait(
                    timeout=liveness_timeout, shutdown=shutdown
                )
                if outcome.is_shutdown:
                    return

                # A TIMEOUT is purely the liveness check WakeSignal.wait
                # documents: it exists to keep the SUB reconcile below
                # alive, and to retry a failed unread refresh (the gate
                # re-marks itself), not to find arrivals.
                if outcome.is_arrival or gate.pending:
                    seen = await _safe_tick(mcp, state, seen, gate=gate)

                # Keep the always-on SUBs bound to the live client — AFTER the
                # tick, not before. The tick's relay calls are what can trigger
                # the wedge teardown (_force_reconnect / _on_closed) that
                # advances connection_generation and orphans a SUB on the dead
                # client. Reconciling here rebinds a same-tick client swap
                # immediately. Also retries a failed initial subscribe (NATS
                # down at startup) and re-subscribes after a client replacement
                # whose new client carries no SUB. Cheap: a generation compare
                # when nothing changed.
                talk_sub = await _reconcile_talk_sub(state, talk_sub, talk_latch, gate)
                inbox_notify_sub = await _reconcile_inbox_notify_sub(
                    state, inbox_notify_sub, inbox_notify_latch, gate
                )
                wall_notify_sub = await _reconcile_wall_notify_sub(
                    state, wall_notify_sub, wall_notify_latch, gate
                )
        finally:
            # A hard cancel arriving while unsubscribing one SUB must not skip
            # the rest — each attempt is shielded from that specific
            # cancellation (the underlying unsubscribe keeps running server-side
            # even if this task stops waiting for it) and both Exception and
            # CancelledError are swallowed here so the loop always reaches every
            # remaining SUB. This is a deliberate, narrow exception to "always
            # re-raise CancelledError": we are already inside the finally block
            # reacting to the original cancellation, and teardown must stay
            # bounded and complete rather than abandoning it partway through.
            all_subs = (talk_sub, inbox_notify_sub, wall_notify_sub)
            for sub in all_subs:
                if sub is not None:
                    with suppress(Exception, asyncio.CancelledError):
                        await asyncio.shield(sub.handle.unsubscribe())
