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

from biff.formatting import terminal_safe
from biff.models import Message
from biff.nats_relay import NatsRelay
from biff.server.tools._activate import auto_enable
from biff.server.tools._descriptions import (
    TALK_BASE_DESCRIPTION,
    get_tty_name,
    refresh_talk,
)
from biff.server.tools._session import resolve_talk_target, update_current_session
from biff.talk_types import TalkPhase
from biff.tty import parse_address

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState
    from biff.talk_types import AgentDrain

logger = logging.getLogger(__name__)

_NO_MESSAGES = "No pending talk activity."
_MAX_BODY = 512


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
    """Render a drained agent snapshot as human-readable talk output.

    Lists pending invites (who wants to talk, with a runnable accept
    command that names the inviter's session) followed by any queued
    messages and a hangup line.  Empty when there is nothing to show.
    """
    lines: list[str] = []
    for _user, invite in sorted(drain.pending.items()):
        lines.append(
            f"📞 {terminal_safe(invite.user)} wants to talk — "
            f"{terminal_safe(invite.accept_command)} to accept"
        )
    for notif in drain.messages:
        sender = terminal_safe(notif.nfrom)
        sender_tty = terminal_safe(notif.nfrom_tty)
        label = f"{sender}:{sender_tty}" if sender_tty else sender
        if notif.is_end:
            lines.append(f"{label} ended the conversation.")
        elif notif.nbody:
            lines.append(f"{label}: {terminal_safe(notif.nbody)}")
    return "\n".join(lines)


async def _resolve_target(
    state: ServerState, user: str, tty: str | None
) -> tuple[str, str, str | None]:
    """Resolve an address to ``(relay_key, display, target_repo)`` or raise."""
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
    relay_tty: str,
    relay_key: str,
    display: str,
    target_repo: str | None,
    message: str,
) -> str:
    """Accept a pending invite (completing the human's handshake)."""
    talk_state = state.talk
    talk_state.begin_connected(
        partner=user, partner_tty=relay_tty, partner_key=relay_key
    )
    await talk_state.send_accept(
        target_user=user, to_key=relay_key, target_repo=target_repo
    )
    if message:
        await talk_state.send_message(
            target_user=user, to_key=relay_key, body=message, target_repo=target_repo
        )
    await refresh_talk(mcp, state)
    opening = f' Sent: "{terminal_safe(message[:_MAX_BODY])}".' if message else ""
    return (
        f"Connected to {display} — accepted their invite.{opening} "
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

    pending_key = talk_state.consume_pending_invite(user)
    resolve_user, resolve_tty = (user, tty)
    if pending_key is not None:
        resolve_user, _, resolve_tty = pending_key.partition(":")
    try:
        relay_key, display, target_repo = await _resolve_target(
            state, resolve_user, resolve_tty
        )
    except ValueError as exc:
        return str(exc)

    if pending_key is not None:
        return await _accept_invite(
            mcp,
            state,
            user=user,
            relay_tty=resolve_tty or "",
            relay_key=relay_key,
            display=display,
            target_repo=target_repo,
            message=message,
        )

    connected_here = (
        talk_state.phase is TalkPhase.CONNECTED and talk_state.partner_key == relay_key
    )
    if connected_here:
        if not message:
            return f"Already connected to {display}. Provide a message to send."
        await talk_state.send_message(
            target_user=user, to_key=relay_key, body=message, target_repo=target_repo
        )
        await refresh_talk(mcp, state)
        return f'Sent to {display}: "{terminal_safe(message[:_MAX_BODY])}".'

    talk_state.begin_invite(
        partner=user, partner_tty=resolve_tty or "", partner_key=relay_key
    )
    invite_body = message or (
        f"wants to talk — reply with: talk {state.config.user}:{get_tty_name()}"
    )
    await talk_state.send_invite(
        target_user=user, to_key=relay_key, body=invite_body, target_repo=target_repo
    )
    await refresh_talk(mcp, state)
    return f"Invite sent to {display}. When they accept, talk_read shows replies."


async def _do_talk_end(mcp: FastMCP[ServerState], state: ServerState) -> str:
    """Close the active talk session (talk.tex LocalEnd).

    An abandoned invite withdraws (``ntWithdraw``); a live conversation
    hangs up (``end``).  The local reset and description refresh happen
    *before* and *regardless of* the publish: the frame is a best-effort
    core-NATS publish, and a wedged or reconnecting relay must never strand
    the local session in a phantom talk state.  On a transient publish
    failure the peer still clears via the pending-invite time-to-live sweep
    (notification.tex ExpirePendingInvite), so we say so.
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
                await talk_state.send_withdraw(target_user=partner, to_key=partner_key)
            else:
                await talk_state.send_end(target_user=partner, to_key=partner_key)
        except (NatsError, TimeoutError, OSError):
            logger.warning(
                "talk_end publish to %s failed; peer falls back to the TTL sweep",
                partner,
                exc_info=True,
            )
            transient = True
    await refresh_talk(mcp, state)
    if transient:
        return (
            f"Talk session with {partner} ended locally; peer will time out in ~5 min."
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
        await refresh_talk(mcp, state)
        return format_agent_drain(drain) or _NO_MESSAGES

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
        # A pending invite already drained into ``pendingInvites`` (queue empty)
        # is real activity — return it promptly instead of sleeping to timeout.
        while not state.talk.has_activity and loop.time() < deadline:
            await asyncio.sleep(0.25)
        drain = state.talk.drain_for_agent()
        await refresh_talk(mcp, state)
        return format_agent_drain(drain) or _NO_MESSAGES

    @mcp.tool(name="talk_end", description="End the current talk session.")
    @auto_enable(state)
    async def talk_end() -> str:
        """Close the active talk session, sending an end frame if connected."""
        return await _do_talk_end(mcp, state)
