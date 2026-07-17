"""Real-time conversation tools — ``talk``, ``talk_read``, ``talk_end``.

Talk is ephemeral (BSD ``talk``): frames ride NATS core pub/sub with no
durable inbox.  The MCP server holds a shared :class:`~biff.talk_state.TalkState`
that an always-on subscription feeds (``_descriptions.subscribe_talk``);
these tools drive it:

* ``talk`` — accept a pending invite (completing the human's handshake),
  send a message while connected, or send an invite.  All ephemeral.
* ``talk_read`` — drain the held state and return who wants to talk plus
  any queued messages.  This is the tool the model calls after the
  tool-list-changed push (DES-020/021).
* ``talk_end`` — close the conversation (sends an end frame if connected).
* ``talk_listen`` — the blocking variant for agent-to-agent flows.

Talk is NATS-only.  LocalRelay and DormantRelay return an error message.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from nats.errors import Error as NatsError

from biff.formatting import HEADER_PREFIX, format_talk_end, terminal_safe
from biff.models import Message
from biff.nats_relay import NatsRelay
from biff.server.tools._activate import auto_enable
from biff.server.tools._descriptions import (
    TALK_BASE_DESCRIPTION,
    get_tty_name,
    refresh_talk,
)
from biff.server.tools._session import resolve_talk_target, update_current_session
from biff.talk_types import MAX_BODY_LEN, TalkPhase
from biff.tty import parse_address

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState
    from biff.talk_types import AgentDrain, PendingInvite

logger = logging.getLogger(__name__)

_NO_MESSAGES = "No pending talk activity."


def format_talk_messages(messages: list[Message]) -> str:
    """Format messages in chat style for talk output."""
    lines: list[str] = []
    for m in messages:
        ts = m.timestamp.strftime("%H:%M:%S")
        lines.append(f"[{ts}] {terminal_safe(m.from_user)}: {terminal_safe(m.body)}")
    return "\n".join(lines)


async def fetch_all_unread(
    relay: NatsRelay, session_key: str, user: str
) -> list[Message]:
    """Fetch and merge unread messages from both inboxes, sorted by time."""
    tty_unread = await relay.fetch(session_key)
    user_unread = await relay.fetch_user_inbox(user)
    return sorted(tty_unread + user_unread, key=lambda m: m.timestamp)


def format_agent_drain(drain: AgentDrain) -> str:
    """Render a drained agent snapshot as talk output for the model's context.

    Lists pending invites (who wants to talk, with a runnable accept
    command that names the inviter's session) followed by any queued
    messages and a hangup line.  Empty when there is nothing to show.

    Shares the ``▶`` who/read/wall idiom with the terminal renders
    (``_format_talk_lines``, ``_format_idle_banners``) but stays
    single-line on purpose: this text is injected into the *model's*
    context, not printed to an 80-column terminal, so it deliberately
    skips ``format_talk_line``'s ``textwrap`` wrapping and hang-indent
    continuation whitespace — alignment padding that aids a human reader
    but is only noise in model input.  Every field is length-clamped at
    the :meth:`TalkNotification.from_payload` ingress boundary, so a
    single line stays bounded without a render-side cap (biff-7g7).
    """
    lines: list[str] = []
    for _user, invite in sorted(drain.pending.items()):
        lines.append(
            f"{HEADER_PREFIX}{terminal_safe(invite.user)} wants to talk — "
            f"{terminal_safe(invite.accept_command)} to accept"
        )
    for notif in drain.messages:
        label = notif.sender_label  # sender_label already neutralises both halves
        if notif.is_end:
            lines.append(format_talk_end(label))
            continue
        # Skip a body that is empty only *after* neutralisation — a control-only
        # payload must not render a dangling ``label:`` line (biff-7g7).
        if body := terminal_safe(notif.nbody):
            lines.append(f"{HEADER_PREFIX}{label}: {body}")
    return "\n".join(lines)


async def _resolve_target(
    state: ServerState, user: str, tty: str | None
) -> tuple[str, str]:
    """Resolve an address to ``(relay_key, display)`` or raise."""
    all_sessions = await state.relay.get_sessions_for_repos(state.visible_repos)
    if not any(s.user == user for s in all_sessions):
        msg = f"{user} is not online."
        raise ValueError(msg)
    return resolve_talk_target(
        all_sessions,
        user,
        tty,
        sender_key=state.session_key,
        sender_repo=state.config.repo_name,
    )


async def _accept_invite(
    mcp: FastMCP[ServerState],
    state: ServerState,
    *,
    user: str,
    pending: PendingInvite,
    relay_key: str,
    display: str,
    resolve_tty: str,
    message: str,
) -> str:
    """Accept a pending invite, refusing if it would clobber a live talk.

    Accepting B's invite while CONNECTED to a *different* peer A (or INVITING a
    different peer) would overwrite the live connection with no end frame — the
    accept-path twin of the new-invite clobber.  The invited session's key is
    *relay_key*; the same-partner cases (a mutual glare completing, or an
    idempotent re-accept of the current partner) share that key and pass.
    """
    talk_state = state.talk
    if talk_state.phase is not TalkPhase.IDLE and talk_state.partner_key != relay_key:
        return (
            f"Already in a talk with {talk_state.partner_display} — "
            "use talk_end (or 'end') first."
        )
    # Consume, but keep the popped invite: a failed accept publish must restore
    # it so a retry re-accepts rather than sending a fresh outbound invite (CR-2).
    consumed = talk_state.consume_pending_invite(user)
    # Name the connected partner by the inviter's DISPLAY tty (``ttyN``), not the
    # session-key hex, so the connected hint reads ``talk @user:ttyN`` — the
    # address ``/who`` shows and ``resolve_talk_target`` matches.
    partner_tty = pending.tty or resolve_tty or ""
    accept_display = f"{user}:{partner_tty}" if partner_tty else display
    talk_state.begin_connected(
        partner=user, partner_tty=partner_tty, partner_key=relay_key
    )
    try:
        await talk_state.send_accept(to_key=relay_key)
        if message:
            await talk_state.send_message(to_key=relay_key, body=message)
    except (NatsError, TimeoutError, OSError):
        # The accept publish failed transiently; roll the phase back to idle and
        # restore the consumed invite so the session is not stranded in a phantom
        # CONNECTED state and the invite stays acceptable on retry.
        talk_state.reset()
        if consumed is not None:
            talk_state.restore_pending_invite(consumed)
        await refresh_talk(mcp, state)
        return f"Could not reach {accept_display} — accept not sent; try again."
    await refresh_talk(mcp, state)
    opening = f' Sent: "{terminal_safe(message[:MAX_BODY_LEN])}".' if message else ""
    return (
        f"Connected to {accept_display} — accepted their invite.{opening} "
        "Use talk_read to see replies, talk_end to close."
    )


async def _do_talk(
    mcp: FastMCP[ServerState], state: ServerState, to: str, message: str
) -> str:
    """Accept an invite, send a message while connected, or send an invite."""
    talk_state = state.talk
    if not isinstance(state.relay, NatsRelay):
        return "Talk requires a NATS relay connection."
    await update_current_session(state)
    user, tty = parse_address(to)

    # Peek, do not consume yet: resolving the target can fail (offline,
    # ambiguous tty), and consuming before resolution would strand an invite
    # that could no longer be accepted.  Consume only once resolution succeeds.
    pending = talk_state.pending_invites.get(user)
    resolve_user, resolve_tty = (user, tty)
    if pending is not None:
        resolve_user, _, resolve_tty = pending.session_key.partition(":")
    try:
        relay_key, display = await _resolve_target(state, resolve_user, resolve_tty)
    except ValueError as exc:
        return str(exc)

    if pending is not None:
        # Re-peek after the resolve await (which yields to the loop): the
        # always-on talk subscription or the TTL sweep can supersede or withdraw
        # the invite while _resolve_target runs.  relay_key was resolved from the
        # snapshot's session key; if the current invite no longer names that
        # session, refuse rather than connect to the stale key or consume a newer
        # superseding invite unchecked (CR-3 TOCTOU).
        current = talk_state.pending_invites.get(user)
        if current is None or current.session_key != pending.session_key:
            return f"{user}'s invite changed while connecting — try talk again."
        return await _accept_invite(
            mcp,
            state,
            user=user,
            pending=pending,
            relay_key=relay_key,
            display=display,
            resolve_tty=resolve_tty or "",
            message=message,
        )

    return await _send_or_invite(
        mcp,
        state,
        user=user,
        relay_key=relay_key,
        display=display,
        resolve_tty=resolve_tty or "",
        message=message,
    )


async def _send_or_invite(
    mcp: FastMCP[ServerState],
    state: ServerState,
    *,
    user: str,
    relay_key: str,
    display: str,
    resolve_tty: str,
    message: str,
) -> str:
    """Send to the connected partner, or start a new invite (no pending accept).

    A same-key CONNECTED session sends *message* (a failed publish leaves the
    connection intact — best-effort core NATS, not a teardown).  Otherwise an
    idle session publishes a fresh invite; a non-idle phase to a *different*
    peer refuses rather than clobber the live talk with no end frame.
    """
    talk_state = state.talk
    connected_here = (
        talk_state.phase is TalkPhase.CONNECTED and talk_state.partner_key == relay_key
    )
    if connected_here:
        if not message:
            return f"Already connected to {display}. Provide a message to send."
        try:
            await talk_state.send_message(to_key=relay_key, body=message)
        except (NatsError, TimeoutError, OSError):
            await refresh_talk(mcp, state)
            return f"Could not reach {display} — message not sent; try again."
        await refresh_talk(mcp, state)
        return f'Sent to {display}: "{terminal_safe(message[:MAX_BODY_LEN])}".'

    if talk_state.phase is not TalkPhase.IDLE:
        return (
            f"Already in a talk with {talk_state.partner_display} — "
            "use talk_end (or 'end') first."
        )

    talk_state.begin_invite(
        partner=user, partner_tty=resolve_tty, partner_key=relay_key
    )
    invite_body = message or (
        f"wants to talk — reply with: talk @{state.config.user}:{get_tty_name()}"
    )
    try:
        await talk_state.send_invite(to_key=relay_key, body=invite_body)
    except (NatsError, TimeoutError, OSError):
        # The invite publish failed transiently; roll the phase back to idle so
        # the session is not stranded in a phantom INVITING state with no peer.
        talk_state.reset()
        await refresh_talk(mcp, state)
        return f"Could not reach {display} — invite not sent; try again."
    await refresh_talk(mcp, state)
    return f"Invite sent to {display}. When they accept, talk_read shows replies."


async def _publish_agent_auto_accept(state: ServerState, drain: AgentDrain) -> bool:
    """Publish the accept a higher-key mutual-glare auto-accept owes; retry once.

    ``drain_for_agent`` transitions the higher-key side to CONNECTED on a mutual
    glare but cannot publish (it is pure state), so the caller must emit the
    accept frame here: the lower-key partner connects ONLY on receiving it
    (talk.tex ``MutualAutoAccept`` — no symmetric fallback), so a dropped accept
    strands the partner and silently drops our messages there.  Retry once before
    giving up, mirroring the human path (``__main__._publish_auto_accept``).

    Returns whether the accept was published.  ``True`` when there was no glare to
    publish; ``False`` only after both attempts fail — the caller surfaces that to
    the agent, which cannot see ``biff.log`` (biff-9la: talk is never silently
    dropped).
    """
    notif = drain.auto_accept
    if notif is None:
        return True
    for attempt in (1, 2):
        try:
            await state.talk.send_accept(to_key=notif.nfrom_key)
        except (NatsError, TimeoutError, OSError):
            logger.info(
                "agent auto-accept to %s failed (attempt %d/2)",
                notif.nfrom,
                attempt,
                exc_info=True,
            )
        else:
            return True
    return False


def _agent_drain_output(drain: AgentDrain, *, accept_published: bool) -> str:
    """Render the agent drain, appending a warning if the accept never went out.

    On a mutual-glare auto-accept the drain connects us but shows nothing about
    the consumed invite, so a failed accept publish would otherwise leave the
    agent believing it is connected while the partner strands.  Surface the
    failure in the returned text — the only channel the agent operator can see.
    """
    text = format_agent_drain(drain) or _NO_MESSAGES
    if accept_published:
        return text
    notif = drain.auto_accept
    partner = notif.nfrom if notif is not None else "the partner"
    if notif is not None and notif.nfrom_tty:
        partner = f"{notif.nfrom}:{notif.nfrom_tty}"
    warning = (
        f"⚠ Couldn't confirm {terminal_safe(partner)} joined the talk — they may "
        "not have connected; send a message or talk_end and retry."
    )
    return f"{text}\n{warning}"


async def _do_talk_end(mcp: FastMCP[ServerState], state: ServerState) -> str:
    """Close the active talk session (talk.tex LocalEnd).

    An abandoned invite withdraws (``ntWithdraw``); a live conversation
    hangs up (``end``).  The local reset and description refresh happen
    *before* and *regardless of* the publish: the frame is a best-effort
    core-NATS publish, and a wedged or reconnecting relay must never strand
    the local session in a phantom talk state.  A transient failure has
    different consequences per phase: a lost *withdraw* still clears the
    invitee via the pending-invite time-to-live sweep (notification.tex
    ExpirePendingInvite), but a lost *end* has no such recovery — the TTL
    sweep reaps pending invites only, never a CONNECTED session, so the peer
    may stay connected until it next interacts.  The returned text names the
    real per-phase outcome rather than promising a timeout that does not apply.
    """
    talk_state = state.talk
    if talk_state.phase is TalkPhase.IDLE:
        return "No active talk session."
    partner = talk_state.partner
    partner_key = talk_state.partner_key
    was_inviting = talk_state.phase is TalkPhase.INVITING
    talk_state.reset()
    transient = False
    if isinstance(state.relay, NatsRelay):
        try:
            if was_inviting:
                await talk_state.send_withdraw(to_key=partner_key)
            else:
                await talk_state.send_end(to_key=partner_key)
        except (NatsError, TimeoutError, OSError):
            # INFO, not WARNING: the CLI raises the stderr handler to WARNING,
            # so a WARNING here would dump this best-effort-publish traceback
            # into the interactive REPL.
            recovery = (
                "invitee falls back to the pending-invite TTL sweep"
                if was_inviting
                else "no TTL sweep for a connected session; peer may stay connected"
            )
            logger.info(
                "talk_end publish to %s failed; %s",
                partner,
                recovery,
                exc_info=True,
            )
            transient = True
    await refresh_talk(mcp, state)
    if transient:
        if was_inviting:
            return (
                f"Talk invite to {partner} withdrawn locally; "
                "their pending invite times out in ~5 min."
            )
        return (
            f"Talk session with {partner} ended locally, but reaching them failed — "
            "they may not know the talk ended; send nothing further or ask them to "
            "run talk_end."
        )
    return f"Talk session with {partner} ended."


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register talk tools."""

    @mcp.tool(
        name="talk",
        description=TALK_BASE_DESCRIPTION,
        meta={"anthropic/alwaysLoad": True},
    )
    @auto_enable(state)
    async def talk(to: str, message: str = "") -> str:
        """Accept an invite, send a message, or invite a teammate to talk.

        ``to`` is an address like ``@user`` or ``@user:tty``.  If that user
        already invited you, this accepts (completing their handshake) and
        sends *message* as the opening line.  If you are already connected,
        *message* is sent.  Otherwise this sends an invite.  All frames are
        ephemeral — no durable inbox.  Use ``talk_read`` to see replies.
        """
        return await _do_talk(mcp, state, to, message)

    @mcp.tool(
        name="talk_read",
        description=(
            "Show pending talk invites and queued talk messages held by the "
            "server, and mark them read. Call this after a talk notification."
        ),
    )
    @auto_enable(state)
    async def talk_read() -> str:
        """Drain and return the held ephemeral talk state.

        Returns who wants to talk (with the accept hint) plus any queued
        messages.  Reads from the server-held ``TalkState`` — never the
        durable inbox — so an unsolicited invite is surfaced even to a
        fresh agent (biff-9la).
        """
        if not isinstance(state.relay, NatsRelay):
            return "Talk requires a NATS relay connection."
        await update_current_session(state)
        drain = state.talk.drain_for_agent()
        published = await _publish_agent_auto_accept(state, drain)
        await refresh_talk(mcp, state)
        return _agent_drain_output(drain, accept_published=published)

    @mcp.tool(
        name="talk_listen",
        description=(
            "Block until talk activity arrives (agent-to-agent). Human "
            "sessions are prompted to call talk_read by the tool list instead."
        ),
    )
    @auto_enable(state)
    async def talk_listen(timeout: int = 30) -> str:
        """Block until the held talk state has activity or *timeout* expires."""
        if not isinstance(state.relay, NatsRelay):
            return "Talk requires a NATS relay connection."
        await update_current_session(state)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(timeout)
        # Block until there is new drainable traffic — queued frames or a
        # pending invite.  A bare open connection is not, by itself, activity,
        # so an active conversation with an empty queue keeps waiting for the
        # partner's next frame instead of returning at once.
        while not state.talk.has_pending_traffic and loop.time() < deadline:
            await asyncio.sleep(0.25)
        drain = state.talk.drain_for_agent()
        published = await _publish_agent_auto_accept(state, drain)
        await refresh_talk(mcp, state)
        return _agent_drain_output(drain, accept_published=published)

    @mcp.tool(name="talk_end", description="End the current talk session.")
    @auto_enable(state)
    async def talk_end() -> str:
        """Close the active talk session, sending an end frame if connected."""
        return await _do_talk_end(mcp, state)
