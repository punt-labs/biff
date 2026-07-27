"""Shared talk orchestration — one kernel, two front-ends.

The MCP ``talk``/``talk_end`` tools and the REPL modal loop both drive the same
ephemeral :class:`~biff.talk_state.TalkState`.  Before this module the accept /
invite / send / end decision logic was copied into both, and the copies drifted
(a trigger wired twice, biff-9la).  Every state-changing talk action now lives
here as a pure async function returning
:class:`~biff.commands._result.CommandResult`; the MCP tool wraps each call with
a ``refresh_talk`` description push, and the REPL wraps them in its interactive
prompt/plan/modal-loop shell (PL-PA-3, DES-022).

The functions never touch the MCP description cache or the REPL prompt — those
are front-end concerns.  They mutate the shared ``TalkState`` and publish
ephemeral core-NATS frames (a no-op on a non-NATS relay), returning the text and
error flag each front-end renders in its own idiom.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from nats.errors import Error as NatsError

from biff.commands._result import CommandResult
from biff.formatting import terminal_safe
from biff.server.tools._session import resolve_talk_target
from biff.talk_types import MAX_BODY_LEN, TalkPhase
from biff.tty import format_address, parse_address

if TYPE_CHECKING:
    from biff.models import BiffConfig
    from biff.relay import Relay
    from biff.talk_state import TalkState
    from biff.talk_types import PendingInvite

logger = logging.getLogger(__name__)


class TalkContext(Protocol):
    """The session context a talk action needs — CliContext and ServerState fit.

    Both front-end contexts compose a mutable ``TalkState`` and expose the relay,
    identity, and repo visibility a talk action reads.  Declaring the shape
    structurally (PY-TS-6) lets one kernel serve both without either importing
    the other's context class.  The members are read-only: both contexts are
    frozen dataclasses, so the Protocol must not demand settable attributes.
    """

    @property
    def talk(self) -> TalkState: ...

    @property
    def relay(self) -> Relay: ...

    @property
    def config(self) -> BiffConfig: ...

    @property
    def session_key(self) -> str: ...

    @property
    def visible_repos(self) -> frozenset[str]: ...


async def resolve_target(
    ctx: TalkContext, user: str, tty: str | None
) -> tuple[str, str]:
    """Resolve an address to ``(relay_key, display)``; raise if the user is offline.

    Talk is session-scoped (DES-043): the address must name a live session.
    ``resolve_talk_target`` maps a ``user:tty`` (or friendly ``tty_name``) to the
    peer's identity key, refusing a bare user or the caller's own session.
    """
    all_sessions = await ctx.relay.get_sessions_for_repos(ctx.visible_repos)
    if not any(s.user == user for s in all_sessions):
        msg = f"{user} is not online."
        raise ValueError(msg)
    return resolve_talk_target(
        all_sessions,
        user,
        tty,
        sender_key=ctx.session_key,
        sender_repo=ctx.config.repo_name,
    )


async def accept_invite(
    ctx: TalkContext,
    *,
    user: str,
    pending: PendingInvite,
    relay_key: str,
    display: str,
    resolve_tty: str,
    message: str,
) -> CommandResult:
    """Accept a pending invite, refusing if it would clobber a live talk.

    Accepting B's invite while CONNECTED to a *different* peer A (or INVITING a
    different peer) would overwrite the live connection with no end frame — the
    accept-path twin of the new-invite clobber.  The invited session's key is
    *relay_key*; the same-partner cases (a mutual glare completing, or an
    idempotent re-accept of the current partner) share that key and pass.
    """
    talk_state = ctx.talk
    if talk_state.phase is not TalkPhase.IDLE and talk_state.partner_key != relay_key:
        return CommandResult(
            text=(
                f"Already in a talk with {talk_state.partner_display} — "
                "use talk_end (or 'end') first."
            ),
            error=True,
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
        return CommandResult(
            text=f"Could not reach {accept_display} — accept not sent; try again.",
            error=True,
        )
    opening = f' Sent: "{terminal_safe(message[:MAX_BODY_LEN])}".' if message else ""
    return CommandResult(
        text=(
            f"Connected to {accept_display} — accepted their invite.{opening} "
            "Use talk_read to see replies, talk_end to close."
        ),
    )


async def invite(
    ctx: TalkContext,
    *,
    user: str,
    relay_key: str,
    display: str,
    resolve_tty: str,
    message: str,
) -> CommandResult:
    """Start a fresh invite to *user*, refusing to clobber a live talk.

    An idle session publishes an invite; a non-idle phase to a *different* peer
    refuses rather than abandon the live talk with no end frame.  The invite body
    carries a runnable ``talk @me:ttyN`` reply hint unless *message* supplies an
    opening line.
    """
    talk_state = ctx.talk
    if talk_state.phase is not TalkPhase.IDLE:
        return CommandResult(
            text=(
                f"Already in a talk with {talk_state.partner_display} — "
                "use talk_end (or 'end') first."
            ),
            error=True,
        )
    talk_state.begin_invite(
        partner=user, partner_tty=resolve_tty, partner_key=relay_key
    )
    reply_to = format_address(ctx.config.user, talk_state.my_tty_name)
    invite_body = message or f"wants to talk — reply with: talk {reply_to}"
    try:
        await talk_state.send_invite(to_key=relay_key, body=invite_body)
    except (NatsError, TimeoutError, OSError):
        # The invite publish failed transiently; roll the phase back to idle so
        # the session is not stranded in a phantom INVITING state with no peer.
        talk_state.reset()
        return CommandResult(
            text=f"Could not reach {display} — invite not sent; try again.",
            error=True,
        )
    return CommandResult(
        text=f"Invite sent to {display}. When they accept, talk_read shows replies.",
    )


async def send_line(
    ctx: TalkContext, *, to_key: str, display: str, message: str
) -> CommandResult:
    """Send one message to the connected partner (best-effort core-NATS publish).

    A failed publish leaves the connection intact — core NATS is best-effort, not
    a teardown — and returns an actionable "not sent" notice rather than raising.
    An empty *message* is a caller error: there is nothing to send.

    Enforces its own precondition (PY-EH-1): the state must be CONNECTED to
    *to_key*.  Both callers already only reach here while connected (the MCP
    dispatcher's ``connected_here`` branch, the REPL modal loop), but making the
    contract self-checking stops a future caller from silently publishing a
    stray frame while idle or inviting.
    """
    talk_state = ctx.talk
    if talk_state.phase is not TalkPhase.CONNECTED or talk_state.partner_key != to_key:
        return CommandResult(
            text=f"Not connected to {display} — nothing sent.",
            error=True,
        )
    if not message:
        return CommandResult(
            text=f"Already connected to {display}. Provide a message to send.",
            error=True,
        )
    try:
        await ctx.talk.send_message(to_key=to_key, body=message)
    except (NatsError, TimeoutError, OSError):
        return CommandResult(
            text=f"Could not reach {display} — message not sent; try again.",
            error=True,
        )
    return CommandResult(
        text=f'Sent to {display}: "{terminal_safe(message[:MAX_BODY_LEN])}".',
    )


async def end_or_cancel(ctx: TalkContext) -> CommandResult:
    """Close the active talk session (talk.tex LocalEnd).

    An abandoned invite withdraws (``ntWithdraw``); a live conversation hangs up
    (``end``).  The local reset and the returned text happen *before* and
    *regardless of* the publish: the frame is a best-effort core-NATS publish, and
    a wedged or reconnecting relay must never strand the local session in a
    phantom talk state.  A transient failure has different consequences per phase:
    a lost *withdraw* still clears the invitee via the pending-invite TTL sweep
    (notification.tex ExpirePendingInvite), but a lost *end* has no such recovery
    — the sweep reaps pending invites only, never a CONNECTED session, so the peer
    may stay connected until it next interacts.  The returned text names the real
    per-phase outcome rather than promising a timeout that does not apply.
    """
    talk_state = ctx.talk
    if talk_state.phase is TalkPhase.IDLE:
        return CommandResult(text="No active talk session.")
    partner = talk_state.partner
    partner_key = talk_state.partner_key
    was_inviting = talk_state.phase is TalkPhase.INVITING
    talk_state.reset()
    transient = False
    # The publish is a no-op on a non-NATS relay (``TalkState._publish`` guards
    # the transport), so it is issued unconditionally — no relay-type gate here.
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
    if transient:
        # The local session ended, but the peer was not notified — signal it as
        # an error so library/CLI callers see a non-zero outcome, consistent with
        # the publish-failure returns of invite/accept_invite/send_line.
        if was_inviting:
            return CommandResult(
                text=(
                    f"Talk invite to {partner} withdrawn locally; "
                    "their pending invite times out in ~5 min."
                ),
                error=True,
            )
        return CommandResult(
            text=(
                f"Talk session with {partner} ended locally, but reaching them "
                "failed — they may not know the talk ended; send nothing further "
                "or ask them to run talk_end."
            ),
            error=True,
        )
    return CommandResult(text=f"Talk session with {partner} ended.")


async def publish_auto_accept(ctx: TalkContext, *, to_key: str) -> bool:
    """Publish the accept a mutual-invite glare owes; retry once before giving up.

    On simultaneous mutual invites the ``keyBelow`` tie-break makes the higher key
    auto-accept and publish an accept; the lower-key partner connects ONLY on
    receiving that frame (talk.tex ``MutualAutoAccept`` — no symmetric fallback),
    so a dropped accept strands the partner and silently discards our subsequent
    messages on their side.  Retry once before giving up.  Returns whether the
    accept was published.
    """
    for attempt in (1, 2):
        try:
            await ctx.talk.send_accept(to_key=to_key)
        except (NatsError, TimeoutError, OSError):
            # INFO, not WARNING: the CLI floors stderr at WARNING, so a WARNING
            # here would dump this best-effort-publish traceback into the REPL.
            logger.info(
                "talk auto-accept to %s failed (attempt %d/2)",
                to_key,
                attempt,
                exc_info=True,
            )
        else:
            return True
    return False


async def talk(ctx: TalkContext, to: str, message: str) -> CommandResult:
    """Accept an invite, send a message while connected, or start an invite.

    The single non-interactive dispatcher the MCP ``talk`` tool calls.  It peeks a
    pending invite from *to*'s user (resolving against the inviter's session when
    one exists), then routes to :func:`accept_invite`, :func:`send_line`, or
    :func:`invite`.  A supersession during the resolve await is refused rather
    than accepted against the stale key (CR-3 TOCTOU).
    """
    talk_state = ctx.talk
    user, tty = parse_address(to)

    # Peek, do not consume yet: resolving the target can fail (offline, ambiguous
    # tty), and consuming before resolution would strand an invite that could no
    # longer be accepted.  Consume only once resolution succeeds.
    pending = talk_state.pending_invites.get(user)
    resolve_user, resolve_tty = (user, tty)
    if pending is not None:
        resolve_user, _, resolve_tty = pending.session_key.partition(":")
    try:
        relay_key, display = await resolve_target(ctx, resolve_user, resolve_tty)
    except ValueError as exc:
        return CommandResult(text=str(exc), error=True)

    if pending is not None:
        # Re-peek after the resolve await (which yields to the loop): the
        # always-on talk subscription or the TTL sweep can supersede or withdraw
        # the invite while resolve_target runs.  relay_key was resolved from the
        # snapshot's session key; if the current invite no longer names that
        # session, refuse rather than connect to the stale key or consume a newer
        # superseding invite unchecked (CR-3 TOCTOU).
        current = talk_state.pending_invites.get(user)
        if current is None or current.session_key != pending.session_key:
            return CommandResult(
                text=f"{user}'s invite changed while connecting — try talk again.",
                error=True,
            )
        return await accept_invite(
            ctx,
            user=user,
            pending=pending,
            relay_key=relay_key,
            display=display,
            resolve_tty=resolve_tty or "",
            message=message,
        )

    connected_here = (
        talk_state.phase is TalkPhase.CONNECTED and talk_state.partner_key == relay_key
    )
    if connected_here:
        return await send_line(ctx, to_key=relay_key, display=display, message=message)
    return await invite(
        ctx,
        user=user,
        relay_key=relay_key,
        display=display,
        resolve_tty=resolve_tty or "",
        message=message,
    )
