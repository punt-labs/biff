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

The accept / invite / send / end decision logic lives once in
:mod:`biff.commands.talk`; these tools wrap each shared action with a
``refresh_talk`` description push and render the returned ``CommandResult``.

Talk is NATS-only.  LocalRelay and DormantRelay return an error message.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import biff.commands.talk as talk_commands
from biff.formatting import (
    HEADER_PREFIX,
    format_talk_end,
    format_talk_line,
    terminal_safe,
    visible_text,
)
from biff.models import Message
from biff.nats_relay import NatsRelay
from biff.server.tools._activity import track_activity
from biff.server.tools._descriptions import (
    TALK_BASE_DESCRIPTION,
    refresh_talk,
)
from biff.server.tools._session import update_current_session

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState
    from biff.talk_types import AgentDrain

logger = logging.getLogger(__name__)

_NO_MESSAGES = "No pending talk activity."


def format_talk_messages(messages: list[Message]) -> str:
    """Format messages in the shared who/read/wall ``▶`` idiom for talk output.

    Reuses :func:`format_talk_line`'s wrap/hang-indent treatment instead of
    embedding each up-to-512-char, potentially CJK/emoji-heavy body on one
    unbounded line — the same defect class fixed for who/read/wall/finger
    (biff-2sw).
    """
    lines: list[str] = []
    for m in messages:
        stamp = f"[{m.timestamp.strftime('%H:%M:%S')}] "
        sender = f"{m.from_user}:{m.from_tty}" if m.from_tty else m.from_user
        lines.extend(format_talk_line(sender, m.body, stamp=stamp))
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

    A frame with no body at all (an accept, or an invite sent with no
    opening line) contributes no line — there is nothing to report.  A
    frame whose body is merely *invisible* after neutralisation
    (whitespace-only or control-only) still contributes a line naming the
    sender: the agent must see that a message arrived even when it had
    nothing visible to show, matching :func:`~biff.formatting.format_talk_line`
    on the terminal side (biff-7g7, biff-2sw round 6).
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
        if not notif.nbody:
            continue
        lines.append(f"{HEADER_PREFIX}{label}: {visible_text(notif.nbody)}")
    return "\n".join(lines)


async def _publish_agent_auto_accept(state: ServerState, drain: AgentDrain) -> bool:
    """Publish the accept a higher-key mutual-glare auto-accept owes.

    ``drain_for_agent`` transitions the higher-key side to CONNECTED on a mutual
    glare but cannot publish (it is pure state), so the caller must emit the
    accept frame: the lower-key partner connects ONLY on receiving it
    (talk.tex ``MutualAutoAccept``), so a dropped accept strands the partner and
    silently drops our messages there.  Delegates the retry-once publish to the
    shared kernel.

    Returns whether the accept was published.  ``True`` when there was no glare to
    publish; ``False`` only after both attempts fail — the caller surfaces that to
    the agent, which cannot see ``biff.log`` (biff-9la: talk is never silently
    dropped).
    """
    notif = drain.auto_accept
    if notif is None:
        return True
    return await talk_commands.publish_auto_accept(state, to_key=notif.nfrom_key)


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


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register talk tools."""

    @mcp.tool(
        name="talk",
        description=TALK_BASE_DESCRIPTION,
        meta={"anthropic/alwaysLoad": True},
    )
    @track_activity(state)
    async def talk(to: str, message: str = "") -> str:
        """Accept an invite, send a message, or invite a teammate to talk.

        ``to`` is an address like ``user`` or ``user:tty``.  If that user
        already invited you, this accepts (completing their handshake) and
        sends *message* as the opening line.  If you are already connected,
        *message* is sent.  Otherwise this sends an invite.  All frames are
        ephemeral — no durable inbox.  Use ``talk_read`` to see replies.
        """
        if not isinstance(state.relay, NatsRelay):
            return "Talk requires a NATS relay connection."
        await update_current_session(state)
        result = await talk_commands.talk(state, to, message)
        await refresh_talk(mcp, state)
        return result.text

    @mcp.tool(
        name="talk_read",
        description=(
            "Show pending talk invites and queued talk messages held by the "
            "server, and mark them read. Call this after a talk notification."
        ),
    )
    @track_activity(state)
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
    @track_activity(state)
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
    @track_activity(state)
    async def talk_end() -> str:
        """Close the active talk session, sending an end frame if connected."""
        result = await talk_commands.end_or_cancel(state)
        await refresh_talk(mcp, state)
        return result.text
