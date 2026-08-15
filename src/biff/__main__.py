"""Biff CLI entry point.

Two modes, one session lifecycle::

    biff              # Interactive REPL (like python3)
    biff who          # Inline command (like python3 -c "...")

Product commands (``biff who``, ``biff finger``, ``biff write``,
``biff read``, ``biff plan``, ``biff last``, ``biff wall``, ``biff mesg``,
``biff tty``, ``biff status``, ``biff talk``), admin commands
(``biff serve``, ``biff enable``, ``biff disable``, ``biff install``,
``biff doctor``, ``biff uninstall``), and status line management.

Every product command is also available as an MCP tool — the CLI is the
complete product, MCP tools are projections of CLI functionality.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue as queue_mod
import sys
import threading as threading_mod
import warnings
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Self, final

import typer
from nats.errors import Error as NatsError

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from nats.aio.client import Client as NatsClient

    from biff.server.state import ServerState

import biff.commands.talk as talk_commands
from biff import commands
from biff.cli_session import CliContext, cli_session
from biff.commands import CommandResult
from biff.commands._session import update_current_session
from biff.config import (
    find_git_root,
    is_enabled,
    load_mcp_config,
)
from biff.formatting import format_talk_end, format_talk_line
from biff.hook import hook_app
from biff.nats_relay import NatsRelay
from biff.repl_display import ReplDisplay
from biff.server.app import create_server
from biff.server.state import create_state
from biff.talk_latch import TalkNotifyLatch
from biff.talk_types import MAX_BODY_LEN, AcceptOutcome, TalkNotification

# ---------------------------------------------------------------------------
# Global flags
#
# Global flags (--json, --verbose, --quiet) go before the subcommand,
# following beads convention: ``biff --json who``, not ``biff who --json``.
# ---------------------------------------------------------------------------

_json_output = False
_quiet_output = False
_user_override: str | None = None


def _print_json(data: object) -> None:
    """Print JSON to stdout."""
    print(json.dumps(data, indent=2, default=str))


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------


class _EofReceivedFilter(logging.Filter):
    """Drop asyncio's 'eof_received' warning from NATS SSL disconnect."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.msg if isinstance(record.msg, str) else record.getMessage()
        return "eof_received" not in msg


_eof_filter_installed = False


def _install_eof_received_filter() -> None:
    """Add the filter to the asyncio logger exactly once."""
    global _eof_filter_installed
    if _eof_filter_installed:
        return
    logging.getLogger("asyncio").addFilter(_EofReceivedFilter())
    _eof_filter_installed = True


def _suppress_nats_noise() -> None:
    """Suppress nats.py noise common to all CLI invocations.

    Floor ``biff.nats_relay`` at INFO, not ERROR.  The two handler levels
    already split terminal from file — stderr shows WARNING+, the file records
    INFO+ (logging_config).  Capping the logger at ERROR defeated that split:
    it dropped every transient connection log (disconnect, reconnect, wedge,
    error_cb) from the FILE too, while the one ERROR-level line (error_cb)
    still cleared the stderr floor and dumped a traceback into the interactive
    REPL.  At INFO the transient events — all demoted to INFO in
    nats_relay — reach biff.log for diagnosis and stay off the terminal, while
    genuine WARNING+ anomalies (malformed messages) still surface.
    """
    warnings.filterwarnings("ignore", category=DeprecationWarning, module="nats")
    logging.getLogger("biff.nats_relay").setLevel(logging.INFO)
    _install_eof_received_filter()


app = typer.Typer(help="Biff: the dog that barked when messages arrived.")
app.add_typer(hook_app, name="hook")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    json_flag: Annotated[
        bool,
        typer.Option("--json", help="Output JSON instead of human-readable text."),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Debug logging to stderr."),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Suppress product command output."),
    ] = False,
    user: Annotated[
        str | None,
        typer.Option("--user", help="Identity override (e.g. for CI bots)."),
    ] = None,
) -> None:
    """Biff: team communication for software engineers."""
    if verbose and quiet:
        raise typer.BadParameter("--verbose and --quiet are mutually exclusive.")

    global _json_output, _quiet_output, _user_override
    _json_output = json_flag
    _quiet_output = quiet
    _user_override = user

    from biff.logging_config import configure_logging

    configure_logging(stderr_level="DEBUG" if verbose else "WARNING")
    _suppress_nats_noise()

    if ctx.invoked_subcommand is None:
        unsupported: list[str] = []
        if _json_output:
            unsupported.append("--json")
        if _quiet_output:
            unsupported.append("--quiet")
        if unsupported:
            flags = " and ".join(unsupported)
            verb = "is" if len(unsupported) == 1 else "are"
            raise typer.BadParameter(f"{flags} {verb} not supported in REPL mode.")
        # No subcommand → launch the REPL.
        asyncio.run(_repl())


# ---------------------------------------------------------------------------
# REPL — interactive command loop
# ---------------------------------------------------------------------------


def _release_prompt(prompt_gate: threading_mod.Event) -> None:
    """Flush stdout, then open the prompt gate.

    The stdin thread prints the next prompt via ``input()`` the instant the
    gate opens, and ``input()`` flushes immediately.  Any buffered stdout must
    reach the terminal first, or the prompt overtakes it and collides with the
    last line of command output.  Routing every gate release through this
    helper keeps the flush and the release inseparable — a print added before a
    future ``prompt_gate.set()`` cannot reintroduce the race.
    """
    sys.stdout.flush()
    prompt_gate.set()


def _handle_timestamps(args: list[str], repl_display: ReplDisplay) -> None:
    """Handle the REPL-only ``timestamps on|off`` toggle.

    Prints a usage line on bad input, otherwise updates *repl_display* and
    confirms the new state.
    """
    if len(args) != 1 or args[0].lower() not in ("on", "off"):
        print("Usage: timestamps on|off")
        return
    on = args[0].lower() == "on"
    repl_display.set_timestamps(on=on)
    print(f"Timestamps {'on' if on else 'off'}.")


def _format_idle_banners(
    notifs: list[TalkNotification],
    # None keeps the historical timestamp-free banner for callers/tests that
    # predate the display toggle — see ReplDisplay.
    display: ReplDisplay | None = None,
) -> list[str]:
    """Format drained idle-mode notifications as REPL banner lines.

    Every bodied frame — invite or idle-arriving message — renders as a
    yellow ``▶`` line in the shared who/read/wall idiom, honouring the
    timestamp toggle.  Accepts are silent (the handshake owns them).  The
    pending-invite bookkeeping lives in :meth:`TalkState.drain_idle`; this
    is pure presentation.
    """
    lines: list[str] = []
    for notif in notifs:
        # Accepts are silent (the handshake owns them); every other bodied
        # frame — invite or idle-arriving message — renders as a yellow ▶
        # line in the shared who/read/wall idiom.
        if notif.is_accept or not notif.nbody:
            continue
        stamp = display.stamp(datetime.now(UTC)) if display is not None else ""
        lines.extend(
            f"\033[1;33m{line}\033[0m"
            for line in format_talk_line(notif.sender_label, notif.nbody, stamp=stamp)
        )
    return lines


async def _poll_notify(
    ctx: CliContext,
    notify: object,
    prompt: str,
    *,
    inline: bool = False,
    display: ReplDisplay | None = None,
) -> None:
    """Check for notification changes and print if any."""
    from biff.repl_notify import NotifyState

    if not isinstance(notify, NotifyState):
        return
    notes: list[str] = []
    try:
        summary = await ctx.relay.get_unread_summary(ctx.session_key)
        wall_post = await ctx.relay.get_wall()
        notes = notify.check(summary.count, wall_post)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).debug("Notify check failed", exc_info=True)

    # Age out invites whose inviter never returned and never withdrew, mirroring
    # the server's _active_tick (notification.tex ExpirePendingInvite).  Without
    # this the REPL never reaps a stranded invite, so a crashed inviter's [TALK]
    # marker lingers until restart (CR-4).
    ctx.talk.expire_stale_invites()
    notes.extend(_format_idle_banners(ctx.talk.drain_idle(), display))

    if notes and inline:
        print("\r\033[K", end="")
        for note in notes:
            print(note)
        print(prompt, end="", flush=True)
    elif notes:
        for note in notes:
            print(note)


async def _sync_notify(ctx: CliContext, notify: object) -> None:
    """Sync notification state after a user command to prevent self-notification."""
    from biff.repl_notify import NotifyState

    if not isinstance(notify, NotifyState):
        return
    try:
        summary = await ctx.relay.get_unread_summary(ctx.session_key)
        wall_post = await ctx.relay.get_wall()
        notify.sync(summary.count, wall_post)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).debug("Notify sync failed")


def _format_talk_lines(
    notifs: list[TalkNotification],
    # None keeps the historical timestamp-free rendering for callers (and
    # tests) that predate the display toggle — see ReplDisplay.
    display: ReplDisplay | None = None,
) -> list[str]:
    """Format drained connected-mode notifications as conversation lines.

    Messages render as a cyan ``▶`` line — the who/read/wall idiom, wrapped
    and honouring the timestamp toggle; an end frame renders a dim hangup
    line.  Invites and accepts are already filtered out by
    :meth:`TalkState.drain_connected`.
    """
    lines: list[str] = []
    for notif in notifs:
        if notif.is_end:
            lines.append(f"\033[2m{format_talk_end(notif.sender_label)}\033[0m")
            continue
        if not notif.nbody:
            continue
        stamp = display.stamp(datetime.now(UTC)) if display is not None else ""
        lines.extend(
            f"\033[36m{line}\033[0m"
            for line in format_talk_line(notif.sender_label, notif.nbody, stamp=stamp)
        )
    return lines


def _print_inline_notifications(notes: list[str], prompt: str) -> None:
    """Print notification lines inline, clearing the line and reshowing prompt."""
    if notes:
        print("\r\033[K", end="")
        for note in notes:
            print(note)
        print(prompt, end="", flush=True)


def _print_hangup(notes: list[str]) -> None:
    """Clear the stale prompt and print hangup notification lines."""
    print("\r\033[K", end="")
    for note in notes:
        print(note)


def _render_connected_drain(
    ctx: CliContext, repl_display: ReplDisplay, talk_prompt: str
) -> bool:
    """Drain queued talk frames, render them, and report a remote hangup.

    Returns ``True`` when the partner ended the conversation (the caller
    exits talk mode); otherwise reprints the talk prompt inline.
    """
    notifs, ended = ctx.talk.drain_connected()
    notes = _format_talk_lines(notifs, repl_display)
    if ended:
        _print_hangup(notes)
        return True
    _print_inline_notifications(notes, talk_prompt)
    return False


async def _send_connected_line(
    ctx: CliContext,
    line: str,
    display: str,
    *,
    to_key: str,
) -> bool:
    """Publish a typed talk line; return ``True`` when the loop should break.

    ``end`` hangs up and breaks; any other non-empty line sends a message and
    continues; an empty line is a no-op.  Both publishes are best-effort: a
    wedged or reconnecting relay must never crash the REPL out of ``asyncio.run``
    with a lost line, so a failed publish prints a notice and the loop survives
    (the ``end`` case still breaks to idle; the ``finally`` in ``_repl_talk``
    resets).  A connected hangup has no TTL sweep — the pending-invite sweep
    reaps invites only, never a live session — so a lost ``end`` may leave the
    peer connected until it next interacts; the printed notice says end was not
    sent.  This mirrors the server twin, which catches the same trio intact.
    """
    if line.lower() == "end":
        try:
            await ctx.talk.send_end(to_key=to_key)
        except (NatsError, TimeoutError, OSError):
            print(f"\r\033[KCould not reach {display} — end not sent.")
        return True
    if line:
        # Delegate the connected send to the shared kernel; a transient publish
        # failure surfaces as an actionable notice and keeps the loop alive.
        result = await talk_commands.send_line(
            ctx, to_key=to_key, display=display, message=line
        )
        if result.error:
            print(f"\r\033[K{result.text}")
    return False


async def _repl_talk(
    ctx: CliContext,
    display: str,
    aqueue: asyncio.Queue[str | None],
    notify_event: asyncio.Event,
    prompt_gate: threading_mod.Event,
    current_prompt: list[str],
    repl_prompt: str,
    repl_display: ReplDisplay,
    *,
    to_key: str,
    talk_sub: _ReplTalkSubscription | None = None,
) -> None:
    """Modal talk sub-loop — send lines to target, show incoming messages.

    Runs until the user types ``end`` or the input stream ends (EOF/Ctrl-C).
    Returns control to the REPL loop when done.  Swaps the prompt to
    a talk-specific one and restores the REPL prompt on exit.

    Messages are sent via the shared ``TalkState`` (ephemeral core-NATS
    publish, no inbox) and received by draining it each 2s tick.

    The connected loop runs *instead of* the REPL idle loop, so a wedge
    teardown that swaps the NATS client mid-conversation would orphan the talk
    SUB on the dead client and silently stop incoming partner messages (sends
    still work — they redial).  Reconciling the SUB on each poll tick re-binds
    it regardless of REPL mode; the call is crash-safe via the latch and a
    no-op when the generation is unchanged.
    """
    talk_prompt = f"{ctx.user}:{ctx.tty_name} ▶ "
    current_prompt[0] = talk_prompt

    print(f"Connected to {display}. Type 'end' to return to REPL.\n")
    _release_prompt(prompt_gate)

    # Wake the first tick so the accepter's opening line (preserved by
    # poll_accept) renders through the same drain path as every other
    # incoming message — after Connected, in conversation format.  An empty
    # drain is a harmless no-op (_print_inline_notifications skips no notes).
    notify_event.set()

    try:
        while True:
            result = await _wait_for_input_or_notify(aqueue, notify_event)
            if result is _NO_INPUT:
                notify_event.clear()
                if talk_sub is not None:
                    await talk_sub.reconcile()
                if _render_connected_drain(ctx, repl_display, talk_prompt):
                    break
                continue

            if result is None:
                break
            if not isinstance(result, str):
                break

            if await _send_connected_line(
                ctx,
                result.strip(),
                display,
                to_key=to_key,
            ):
                break
            _release_prompt(prompt_gate)
    finally:
        # Whatever exit path (end, EOF, remote hangup) — return to idle so
        # the REPL's idle drain renders correctly (talk.tex LocalEnd).
        ctx.talk.reset()
        current_prompt[0] = repl_prompt
        # Clear the talk plan when exiting talk mode.
        await _clear_talk_plan(ctx)

    # Clear any stale prompt the stdin thread may have printed.
    print(f"\r\033[KTalk with {display} ended.")


async def _repl_idle_tick(
    ctx: CliContext,
    notify: object,
    prompt: str,
    notify_event: asyncio.Event,
    repl_display: ReplDisplay,
    talk_sub: _ReplTalkSubscription | None,
) -> None:
    """Handle a REPL idle wake: poll for changes, then re-bind the talk SUB.

    The reconcile runs after ``_poll_notify`` so a client replacement the poll
    triggered (a wedge teardown redials a fresh client with no SUB) is picked
    up on the same tick.
    """
    notify_event.clear()
    await _poll_notify(ctx, notify, prompt, inline=True, display=repl_display)
    if talk_sub is not None:
        await talk_sub.reconcile()


async def _repl_loop(
    ctx: CliContext,
    notify: object,
    prompt: str,
    aqueue: asyncio.Queue[str | None],
    notify_event: asyncio.Event,
    prompt_gate: threading_mod.Event,
    current_prompt: list[str],
    *,
    # None → a fresh session-default (timestamps off).  Keeps the many
    # existing positional callers/tests working without threading state.
    display: ReplDisplay | None = None,
    talk_sub: _ReplTalkSubscription | None = None,
) -> None:
    """Core REPL input loop — dispatches commands and handles notifications.

    Talk state lives on ``ctx.talk`` — the shared ``TalkState`` an
    always-on NATS subscription feeds and the idle poll drains.
    """
    from biff.dispatch import dispatch

    repl_display = display if display is not None else ReplDisplay()

    while True:
        result = await _wait_for_input_or_notify(aqueue, notify_event)
        if result is _NO_INPUT:
            await _repl_idle_tick(
                ctx, notify, prompt, notify_event, repl_display, talk_sub
            )
            continue

        if result is None:
            print()
            break
        if not isinstance(result, str):
            break

        line = result

        # Handle talk as a modal command — enters a sub-loop.
        tokens = line.split(None, 2)
        if tokens and tokens[0].lower() == "talk":
            await _handle_repl_talk(
                ctx,
                tokens[1:],
                aqueue,
                notify_event,
                prompt_gate,
                current_prompt,
                prompt,
                repl_display,
                talk_sub=talk_sub,
            )
            _release_prompt(prompt_gate)
            continue

        # REPL-only display toggle (not an MCP tool).
        if tokens and tokens[0].lower() == "timestamps":
            _handle_timestamps(tokens[1:], repl_display)
            _release_prompt(prompt_gate)
            continue

        try:
            cmd_result = await dispatch(line, ctx)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            _release_prompt(prompt_gate)
            continue

        if cmd_result is None:
            break
        if cmd_result.text:
            print(cmd_result.text)

        # Sync state after the user's own command so the next poll
        # doesn't notify about changes the user just made.
        await _sync_notify(ctx, notify)

        # Flush output, then let the stdin thread print the next prompt.
        _release_prompt(prompt_gate)


def _print_talk_banner(notif: TalkNotification) -> None:
    """Print a third-party talk notification in the wrapped ``▶`` idiom."""
    # Render first: only a truly bodiless frame (no body at all) renders no
    # line — a whitespace- or control-only body still renders an explanatory
    # fallback line.  Clearing the prompt before checking would blank the
    # line even on the rare truly-empty case.
    lines = format_talk_line(notif.sender_label, notif.nbody)
    if not lines:
        return
    # Clear the stdin thread's prompt so the banner lands clean, then print.
    print("\r\033[K", end="")
    for line in lines:
        print(f"\033[1;33m{line}\033[0m")


async def _wait_for_talk_accept(
    ctx: CliContext,
    aqueue: asyncio.Queue[str | None],
    notify_event: asyncio.Event,
    prompt_gate: threading_mod.Event,
    *,
    talk_sub: _ReplTalkSubscription | None = None,
) -> AcceptOutcome:
    """Wait for the target to accept, or for a mutual-invite auto-accept.

    Returns the :class:`AcceptOutcome`; ``NONE`` when the user typed
    ``end`` or EOF before any accept arrived.  Third-party notifications
    surfaced by :meth:`TalkState.poll_accept` print as banners.

    The accept wait blocks outside the REPL idle loop, so a wedge teardown
    that swaps the NATS client while we wait would orphan the talk SUB on the
    dead client — the invitee's accept and opening line would never arrive.
    Reconciling the SUB on each poll tick re-binds it regardless; the call is
    crash-safe via the latch and a no-op when the generation is unchanged.
    """
    # Open the prompt gate before waiting so the stdin thread actually calls
    # ``input()`` and reads the user's line.  Without this the thread stays
    # parked at ``prompt_gate.wait()`` and a typed ``end`` never reaches the
    # cancel check below — the same release the connected loop does up front.
    _release_prompt(prompt_gate)
    while True:
        result = await _wait_for_input_or_notify(aqueue, notify_event)
        if result is _NO_INPUT:
            notify_event.clear()
            if talk_sub is not None:
                await talk_sub.reconcile()
            outcome, others = ctx.talk.poll_accept()
            for notif in others:
                _print_talk_banner(notif)
            if outcome is not AcceptOutcome.NONE:
                return outcome
            continue

        if result is None or not isinstance(result, str):
            return AcceptOutcome.NONE
        if result.strip().lower() in ("end", "exit", "quit"):
            return AcceptOutcome.NONE
        _release_prompt(prompt_gate)


async def _set_talk_plan(ctx: CliContext, display: str) -> None:
    """Best-effort set the session plan to ``talking to {display}`` (presence).

    Routes through :func:`update_current_session` so an active REPL talk
    session advances ``last_tool_at`` the same way ``plan``/``tty``/``mesg``
    do -- otherwise a long-lived talk conversation reads as increasingly
    idle the longer it runs, since only the background heartbeat loop would
    otherwise touch this session's timestamps.  A wedged relay must never
    crash the REPL over a cosmetic presence update, so the call is guarded
    and logged at DEBUG.
    """
    try:
        await update_current_session(ctx, plan=f"talking to {display}")
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).debug("Failed to set talk plan", exc_info=True)


async def _clear_talk_plan(ctx: CliContext) -> None:
    """Best-effort clear the talk plan when a talk cancels, withdraws, or ends."""
    try:
        await update_current_session(ctx, plan="")
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).debug("Failed to clear talk plan", exc_info=True)


async def _withdraw_talk_invite(ctx: CliContext) -> None:
    """Withdraw an outstanding invite and clear the talk plan.

    Delegates the reset + ``ntWithdraw`` publish to the shared ``end_or_cancel``
    kernel (best-effort: on a publish failure the invitee still clears via the
    pending-invite TTL sweep, notification.tex ``ExpirePendingInvite``), then
    clears the local ``talking to …`` plan.  Used by the graceful ``end`` cancel
    and the Ctrl-C process-exit path during an outstanding invite.
    """
    await talk_commands.end_or_cancel(ctx)
    await _clear_talk_plan(ctx)


async def _initiate_talk(
    ctx: CliContext,
    *,
    user_target: str,
    target_key: str,
    display: str,
    resolve_tty: str,
    opening: str,
    aqueue: asyncio.Queue[str | None],
    notify_event: asyncio.Event,
    prompt_gate: threading_mod.Event,
    talk_sub: _ReplTalkSubscription | None = None,
) -> bool:
    """Send an invite and wait for the accept; return ``True`` to enter the loop.

    Delegates the phase transition + invite publish (and its rollback on a
    transient publish failure) to the shared ``invite`` kernel, then runs the
    REPL-only interactive wait: a graceful ``end`` cancels (withdraw), a Ctrl-C
    withdraws and re-raises, and a mutual-glare auto-accept publishes the owed
    accept and warns if it never reaches the partner (talk.tex MutualAutoAccept).
    """
    result = await talk_commands.invite(
        ctx,
        user=user_target,
        relay_key=target_key,
        display=display,
        resolve_tty=resolve_tty,
        message=opening,
    )
    if result.error:
        print(result.text)
        return False
    await _set_talk_plan(ctx, display)

    if opening:
        # Echo exactly what was sent: TalkState._publish truncates the invite
        # body to MAX_BODY_LEN, so a long opening's local echo must match.
        print(f"you> {opening[:MAX_BODY_LEN]}")

    # Clear the stdin thread's prompt first so the line lands clean, not
    # appended to a stale ``user:tty ▶`` prompt (same pattern as :303/:311).
    print("\r\033[K", end="")
    print(f"Waiting for {display} to respond... (type 'end' to cancel)")

    # A Ctrl-C during the invite fires the withdraw, then exits the REPL to
    # the shell.  ``asyncio.run`` cancels the main task on SIGINT, so the wait
    # raises ``CancelledError`` — not ``KeyboardInterrupt``, which the runner
    # re-raises only after the task has unwound.  Catch the cancel, withdraw so
    # the invitee's ``[TALK]`` marker clears at once rather than at the TTL
    # sweep, then re-raise so the cancellation propagates and the process exits
    # normally.  ``end``/``exit``/``quit`` is the graceful in-REPL cancel that
    # returns to the prompt (``AcceptOutcome.NONE`` below); Ctrl-C is an exit.
    try:
        outcome = await _wait_for_talk_accept(
            ctx, aqueue, notify_event, prompt_gate, talk_sub=talk_sub
        )
    except (asyncio.CancelledError, KeyboardInterrupt):
        await _withdraw_talk_invite(ctx)
        raise
    if outcome is AcceptOutcome.NONE:
        print(f"Talk with {display} cancelled.")
        await _withdraw_talk_invite(ctx)
        return False
    auto_accept_owed = outcome is AcceptOutcome.AUTO_ACCEPT
    if auto_accept_owed and not await talk_commands.publish_auto_accept(
        ctx, to_key=target_key
    ):
        # The lower-key partner connects ONLY on receiving this accept — there is
        # no symmetric fallback on their side (talk.tex MutualAutoAccept) — so a
        # persistent failure strands them.  poll_accept already advanced us to
        # CONNECTED locally; proceed, but warn that the partner may not have.
        print(
            f"Warning: couldn't confirm {display} joined — they may not have "
            "connected. Send a message or type 'end' and retry."
        )
    return True


async def _handle_repl_talk(
    ctx: CliContext,
    args: list[str],
    aqueue: asyncio.Queue[str | None],
    notify_event: asyncio.Event,
    prompt_gate: threading_mod.Event,
    current_prompt: list[str],
    repl_prompt: str,
    repl_display: ReplDisplay,
    *,
    talk_sub: _ReplTalkSubscription | None = None,
) -> None:
    """Parse talk args and enter modal talk mode.

    The REPL's interactive shell around the shared talk kernel: it resolves the
    session-scoped target, then delegates the accept (responder) or invite +
    accept-wait (initiator) to ``biff.commands.talk``.  On a delegated refusal or
    publish failure it prints the kernel's message and returns; on success it
    sets the presence plan and enters the modal ``_repl_talk`` loop.
    """
    from biff.server.tools._session import resolve_talk_target
    from biff.tty import parse_address

    if not args:
        print("Usage: talk user:ttyN [message]")
        return

    try:
        user_target, tty_target = parse_address(args[0])
    except ValueError as exc:
        print(f"Error: {exc}")
        return

    if not isinstance(ctx.relay, NatsRelay):
        print("Talk requires a NATS relay.")
        return

    all_sessions = await ctx.relay.get_sessions_for_repos(ctx.visible_repos)
    sessions = [s for s in all_sessions if s.user == user_target]
    if not sessions:
        print(f"{user_target} is not online.")
        return

    # Responding to a pending invite targets the exact inviting session;
    # otherwise the address itself must name the session (talk is
    # session-scoped — DES-043).
    #
    # This peek is taken AFTER the sole await (get_sessions_for_repos above);
    # resolve_talk_target is synchronous and accept_invite consumes the invite
    # before its first await, so no await separates this peek from the consume.
    # The always-on talk subscription therefore cannot interleave a supersession
    # here (single-threaded asyncio), and the REPL needs no re-peek — unlike the
    # MCP dispatcher (commands.talk.talk), whose peek precedes its resolve await
    # and so guards the CR-3 TOCTOU window explicitly.
    pending = ctx.talk.pending_invites.get(user_target)
    resolve_user, resolve_tty = (user_target, tty_target)
    if pending is not None:
        resolve_user, _, resolve_tty = pending.session_key.partition(":")
    try:
        target_key, display = resolve_talk_target(
            all_sessions,
            resolve_user,
            resolve_tty,
            sender_key=ctx.session_key,
            sender_repo=ctx.config.repo_name,
        )
    except ValueError as exc:
        print(f"Error: {exc}")
        return

    if pending is not None:
        # Responder: the kernel guards against clobbering a live talk, consumes
        # the invite, publishes the accept, and restores the invite on a transient
        # publish failure (CR-2) — so a refused/failed accept leaves state intact.
        result = await talk_commands.accept_invite(
            ctx,
            user=user_target,
            pending=pending,
            relay_key=target_key,
            display=display,
            resolve_tty=resolve_tty or "",
            message="",
        )
        if result.error:
            print(result.text)
            return
        await _set_talk_plan(ctx, display)
    else:
        opening = " ".join(args[1:]) if len(args) > 1 else ""
        if not await _initiate_talk(
            ctx,
            user_target=user_target,
            target_key=target_key,
            display=display,
            resolve_tty=resolve_tty or "",
            opening=opening,
            aqueue=aqueue,
            notify_event=notify_event,
            prompt_gate=prompt_gate,
            talk_sub=talk_sub,
        ):
            return

    await _repl_talk(
        ctx,
        display,
        aqueue,
        notify_event,
        prompt_gate,
        current_prompt,
        repl_prompt,
        repl_display,
        to_key=target_key,
        talk_sub=talk_sub,
    )


class _TalkSubscription:
    """A generation-tracked, crash-safe talk-notify SUB (nats-relay.tex talkSubGen).

    Both talk front-ends subscribe to the per-user notify subject to wake on an
    incoming frame.  A wedge teardown (``_force_reconnect``) or a
    give-up close drops the NATS client and the next dial builds a fresh one
    with no SUB, orphaning the held handle on the closed client.
    :meth:`reconcile` re-subscribes when the relay dials a new client — detected
    by a bump in ``connection_generation`` — but leaves the SUB untouched on an
    in-place nats-py reconnect, which reuses the client and replays every SUB.

    The base callback only wakes the loop; the standalone ``biff talk`` command
    fetches messages from the durable inbox on that wake, so it needs no frame
    routing.  :class:`_ReplTalkSubscription` overrides the callback to feed the
    always-on REPL ``TalkState`` its ephemeral frames.
    """

    _relay: object
    _session_key: str
    _notify_event: asyncio.Event
    _handle: object | None
    _generation: int
    _latch: TalkNotifyLatch

    def __new__(
        cls, relay: object, session_key: str, notify_event: asyncio.Event
    ) -> Self:
        self = super().__new__(cls)
        self._relay = relay
        self._session_key = session_key
        self._notify_event = notify_event
        self._handle = None
        self._generation = 0
        self._latch = TalkNotifyLatch.for_resubscribe(logging.getLogger(__name__))
        return self

    async def establish(self) -> None:
        """Subscribe on the live client, capturing the generation it binds to.

        A dial in progress (the client mid-replacement) can make ``get_nc`` or
        ``subscribe`` raise; a raise here must not crash the caller and kill the
        retry loop that was meant to self-heal.  On failure, leave the handle
        and generation unchanged so the next ``reconcile`` tick retries, and
        let the latch log the failure once.  The generation binds
        only after a successful subscribe.
        """
        relay = self._relay
        if not isinstance(relay, NatsRelay):
            return
        try:
            nc: NatsClient = await relay.get_nc()
            generation = relay.connection_generation
            subject = relay.talk_notify_subject(self._session_key)
            handle = await nc.subscribe(  # pyright: ignore[reportUnknownMemberType]
                subject, cb=self._on_notify
            )
        except Exception:  # noqa: BLE001
            self._latch.record_failure()
            return
        self._handle = handle
        self._generation = generation
        self._latch.record_success()

    async def reconcile(self) -> None:
        """Re-subscribe when the relay replaced its client since we bound.

        The generation comparison — not a handle-liveness probe — is the
        discriminator the proven model requires: a wedge teardown leaves the
        orphaned handle non-``None``, so an is-None test never fires and talk
        dies silently.  Binds the new generation only on a successful
        re-subscribe.
        """
        relay = self._relay
        if not isinstance(relay, NatsRelay):
            return
        if self._handle is not None and self._generation >= relay.connection_generation:
            return
        await self._unsubscribe()
        await self.establish()

    async def close(self) -> None:
        """Tear the subscription down on caller exit."""
        await self._unsubscribe()

    async def _unsubscribe(self) -> None:
        if self._handle is not None:
            # A superseding dial already closed the client; unsubscribing the
            # orphaned handle is best-effort and its failure is expected.
            with suppress(Exception):
                await self._handle.unsubscribe()  # type: ignore[attr-defined]
            self._handle = None

    async def _on_notify(self, _msg: object) -> None:
        """Wake the conversation loop; the durable inbox carries the message."""
        self._notify_event.set()


@final
class _ReplTalkSubscription(_TalkSubscription):
    """The REPL's always-on talk SUB — feeds ephemeral frames into ``TalkState``.

    Every talk frame flows into ``ctx.talk.receive`` (self-echo and
    session-scope filtering happen there) before the idle drain renders it,
    then wakes the loop.
    """

    _ctx: CliContext

    def __new__(cls, ctx: CliContext, notify_event: asyncio.Event) -> Self:
        self = super().__new__(cls, ctx.relay, ctx.session_key, notify_event)
        self._ctx = ctx
        return self

    async def _on_notify(self, msg: object) -> None:
        data = getattr(msg, "data", b"")
        if data and data != b"1":
            try:
                raw: object = json.loads(data)
                if isinstance(raw, dict):
                    notification: dict[str, str] = {
                        str(k): str(v)  # pyright: ignore[reportUnknownArgumentType]
                        for k, v in raw.items()  # pyright: ignore[reportUnknownVariableType,reportUnknownMemberType]
                    }
                    self._ctx.talk.receive(notification)
            except (json.JSONDecodeError, TypeError):
                logging.getLogger(__name__).debug(
                    "Failed to process talk notification", exc_info=True
                )
        self._notify_event.set()


async def _repl() -> None:
    """Interactive REPL: connect once, run commands, clean up on exit.

    Uses a stdin reader thread so the event loop stays unblocked —
    heartbeat and notification polling run while the user is idle at
    the prompt.  Message notifications are NATS-driven (instant);
    wall changes are detected via 2s timeout polling.

    Readline provides line editing (arrow keys), command history
    (up/down, persisted to ``~/.punt-labs/biff/repl_history``), and tab
    completion for command names.
    """
    from biff.dispatch import available_commands
    from biff.repl_notify import NotifyState
    from biff.repl_readline import setup as setup_readline

    cmds = available_commands()
    setup_readline(cmds)

    try:
        async with cli_session(interactive=True, user_override=_user_override) as ctx:
            print(f"biff {pkg_version('punt-biff')} — {ctx.user}:{ctx.tty_name}")
            print(f"Commands: {', '.join(cmds)}, talk, timestamps, exit")
            print()

            notify = NotifyState()
            prompt = f"{ctx.user}:{ctx.tty_name} ▶ "
            # Mutable prompt container — talk mode swaps the prompt
            # string while reusing the same stdin thread.
            current_prompt = [prompt]
            # Session-scoped display prefs (timestamps toggle); not persisted.
            display = ReplDisplay()

            # Seed initial state without emitting notifications.
            await _sync_notify(ctx, notify)

            # Start stdin reader thread + asyncio bridge.
            input_queue: queue_mod.Queue[str | None] = queue_mod.Queue()
            stop_flag = threading_mod.Event()
            # Gate: the thread waits for this event before printing
            # the prompt and reading the next line. The async loop
            # sets it after command output is complete.
            prompt_gate = threading_mod.Event()
            prompt_gate.set()  # Allow the first prompt immediately.

            def _read_stdin() -> None:
                """Read lines via input(prompt) for full readline support.

                Waits for ``prompt_gate`` before each read so the prompt
                only appears after the async loop has finished printing
                command output.
                """
                while not stop_flag.is_set():
                    prompt_gate.wait()
                    if stop_flag.is_set():
                        return
                    prompt_gate.clear()
                    try:
                        ln = input(current_prompt[0])
                    except (EOFError, KeyboardInterrupt):
                        input_queue.put(None)
                        return
                    input_queue.put(ln)

            threading_mod.Thread(target=_read_stdin, daemon=True).start()
            aqueue: asyncio.Queue[str | None] = asyncio.Queue()
            bridge_task = asyncio.create_task(_bridge_stdin(input_queue, aqueue))

            notify_event = asyncio.Event()
            talk_sub = _ReplTalkSubscription(ctx, notify_event)
            await talk_sub.establish()

            try:
                await _repl_loop(
                    ctx,
                    notify,
                    prompt,
                    aqueue,
                    notify_event,
                    prompt_gate,
                    current_prompt,
                    display=display,
                    talk_sub=talk_sub,
                )
            finally:
                stop_flag.set()
                prompt_gate.set()  # Unblock thread so it sees stop_flag.
                # Unblock the bridge task so it doesn't hang on
                # the stdin reader thread.
                input_queue.put(None)
                bridge_task.cancel()
                with suppress(asyncio.CancelledError):
                    await bridge_task
                await talk_sub.close()
    except KeyboardInterrupt:
        print()
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise typer.Exit(code=1) from None


# ---------------------------------------------------------------------------
# Product commands — CLI projections of MCP tools
#
# Each command delegates to a pure async function in ``biff.commands``
# that returns a ``CommandResult``.  The ``_run()`` adapter handles
# relay session setup, JSON/text branching, and exit codes.
# ---------------------------------------------------------------------------


def _run(
    coro_factory: Callable[[CliContext], Awaitable[CommandResult]],
) -> None:
    """Run a command function inside a CLI session.

    Handles JSON/text branching, stderr for errors, and exit codes.
    """

    async def _inner() -> None:
        try:
            async with cli_session(user_override=_user_override) as ctx:
                result = await coro_factory(ctx)
        except ValueError as exc:
            if _json_output:
                _print_json({"error": str(exc)})
            else:
                print(f"Error: {exc}", file=sys.stderr)
            raise typer.Exit(code=1) from None

        if _json_output:
            data = result.json_data if result.json_data is not None else result.text
            _print_json(data)
        elif result.error:
            print(result.text, file=sys.stderr)
        elif not _quiet_output:
            print(result.text)
        if result.error:
            raise typer.Exit(code=1)

    asyncio.run(_inner())


@app.command()
def who() -> None:
    """List active team members and what they're working on."""
    _run(commands.who)


@app.command()
def finger(
    user: Annotated[str, typer.Argument(help="User to query, e.g. kai or kai:tty1")],
) -> None:
    """Check what a user is working on and their availability."""
    _run(lambda ctx: commands.finger(ctx, user))


@app.command("write")
def write_cmd(
    to: Annotated[str, typer.Argument(help="Recipient, e.g. kai or kai:tty1")],
    message: Annotated[str, typer.Argument(help="Message to send (auto-splits)")],
) -> None:
    """Send a message to a teammate's inbox."""
    _run(lambda ctx: commands.write(ctx, to, message))


@app.command("read")
def read_cmd() -> None:
    """Check inbox for new messages. Marks all as read."""
    _run(commands.read)


@app.command()
def plan(
    message: Annotated[str, typer.Argument(help="What you're working on")] = "",
    clear: Annotated[bool, typer.Option("--clear", help="Clear plan")] = False,
) -> None:
    """Set what you're currently working on."""
    if clear:
        _run(lambda ctx: commands.plan(ctx, ""))
    elif not message:
        print("Usage: biff plan <message> | biff plan --clear", file=sys.stderr)
        raise typer.Exit(code=1)
    else:
        _run(lambda ctx: commands.plan(ctx, message))


@app.command("last")
def last_cmd(
    user: Annotated[str, typer.Argument(help="Filter by user (optional)")] = "",
    count: Annotated[int, typer.Option(help="Number of entries")] = 25,
) -> None:
    """Show session login/logout history."""
    _run(lambda ctx: commands.last(ctx, user, count))


@app.command("wall")
def wall_cmd(
    message: Annotated[str, typer.Argument(help="Broadcast message")] = "",
    duration: Annotated[str, typer.Option(help="Duration (e.g. 30m, 2h, 1d)")] = "",
    clear: Annotated[bool, typer.Option("--clear", help="Remove active wall")] = False,
) -> None:
    """Post, read, or clear a team broadcast."""
    _run(lambda ctx: commands.wall(ctx, message, duration, clear=clear))


@app.command()
def mesg(
    enabled: Annotated[
        str,
        typer.Argument(help="on/off (or y/n) to accept or block messages"),
    ],
) -> None:
    """Control message reception (on/off/y/n)."""
    _run(lambda ctx: commands.mesg(ctx, enabled))


@app.command("tty")
def tty_cmd(
    name: Annotated[str, typer.Argument(help="Session name (optional)")] = "",
) -> None:
    """Name the current CLI session."""
    _run(lambda ctx: commands.tty(ctx, name))


@app.command()
def status() -> None:
    """Show connection state, session info, and pending messages."""
    _run(commands.status)


# ---------------------------------------------------------------------------
# Admin commands
# ---------------------------------------------------------------------------


@app.command("version")
def version() -> None:
    """Print the biff version."""
    ver = pkg_version("punt-biff")
    if _json_output:
        _print_json({"version": ver})
        return
    print(f"biff {ver}")


def _create_mcp_server(
    *,
    user: str | None,
    data_dir: Path | None,
    relay_url: str | None,
    prefix: Path,
) -> FastMCP[ServerState]:
    """Shared config → state → server setup for serve/mcp."""
    from biff.config import RELAY_URL_UNSET
    from biff.session_id import SessionHint
    from biff.session_key import find_session_key
    from biff.statusline import UNREAD_DIR

    resolved = load_mcp_config(
        user_override=user,
        data_dir_override=data_dir,
        relay_url_override=relay_url if relay_url is not None else RELAY_URL_UNSET,
        prefix=prefix,
    )
    dormant = not is_enabled(resolved.repo_root)

    # Route on the Claude session_id: read the SessionStart hook's
    # hint left for this server's claude ancestor.  None outside Claude Code
    # (headless/CI/SDK) — create_state then mints a fresh, misroute-safe hex.
    routing_id = SessionHint.resolve_routing_id()

    # Companion (human) registration is deferred to the heartbeat
    # loop -- the ethos roster is not yet available at startup on
    # claude --resume (spec § 3.2).
    state = create_state(
        resolved.config,
        resolved.data_dir,
        tty=routing_id,
        unread_path=UNREAD_DIR / f"{find_session_key()}.json",
        dormant=dormant,
        repo_root=resolved.repo_root,
        repo_common_root=resolved.repo_common_root,
    )
    return create_server(state)


@app.command()
def serve(
    user: Annotated[
        str | None,
        typer.Option(help="Your username. Auto-detected from GitHub CLI."),
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option(help="Data directory. Auto-computed as {prefix}/biff/{repo}."),
    ] = None,
    relay_url: Annotated[
        str | None,
        typer.Option(help="Relay URL override. Empty string forces local relay."),
    ] = None,
    prefix: Annotated[
        Path,
        typer.Option(help="Base path for data directory (default: /tmp)."),
    ] = Path("/tmp"),  # noqa: S108
    host: Annotated[str, typer.Option(help="HTTP host.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="HTTP port.")] = 8419,
) -> None:
    """Start the biff MCP server (HTTP transport)."""
    server = _create_mcp_server(
        user=user or _user_override,
        data_dir=data_dir,
        relay_url=relay_url,
        prefix=prefix,
    )
    print(f"Starting biff MCP server on http://{host}:{port}")
    server.run(transport="http", host=host, port=port)


@app.command("mcp")
def mcp_cmd(
    user: Annotated[
        str | None,
        typer.Option(help="Your username. Auto-detected from GitHub CLI."),
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option(help="Data directory. Auto-computed as {prefix}/biff/{repo}."),
    ] = None,
    relay_url: Annotated[
        str | None,
        typer.Option(help="Relay URL override. Empty string forces local relay."),
    ] = None,
    prefix: Annotated[
        Path,
        typer.Option(help="Base path for data directory (default: /tmp)."),
    ] = Path("/tmp"),  # noqa: S108
) -> None:
    """Start the biff MCP server (stdio transport)."""
    server = _create_mcp_server(
        user=user or _user_override,
        data_dir=data_dir,
        relay_url=relay_url,
        prefix=prefix,
    )
    server.run(transport="stdio")


@app.command("install-statusline")
def install_statusline() -> None:
    """Install biff into Claude Code's status bar."""
    from biff.statusline import install as do_install

    result = do_install()
    print(result.message)
    if not result.installed:
        raise typer.Exit(code=1)


@app.command("uninstall-statusline")
def uninstall_statusline() -> None:
    """Remove biff from Claude Code's status bar."""
    from biff.statusline import uninstall as do_uninstall

    result = do_uninstall()
    print(result.message)
    if not result.uninstalled:
        raise typer.Exit(code=1)


@app.command()
def enable(
    start: Annotated[
        Path | None,
        typer.Option(help="Repo root (default: auto-detect)."),
    ] = None,
) -> None:
    """Enable biff in the current git repo — fully activates this clone.

    Writes the two committed enablement artifacts (DES-052): the marker
    ``.punt-labs/biff/enabled`` and the CI notify workflow
    ``.github/workflows/biff-notify.yml``, AND deploys this clone's local
    ``.git/hooks`` biff dispatchers so the repo is active here in one verb
    (the beads ``bd setup`` model).  Equivalent to the MCP ``/biff enable``
    toggle — both do exactly this.  Idempotent.  The committed files are
    tracked: commit them via a PR so every contributor participates; the git
    hooks are per-clone and never committed.  Invokes git only read-only (to
    resolve the hooks directory) and never creates commits.
    """
    repo_root = find_git_root(start)
    if repo_root is None:
        raise SystemExit("Not in a git repository. Run this from inside a repo.")

    from biff.enablement import RepoEnablement
    from biff.git_hooks import HOOKS_DIR_UNRESOLVED_NOTICE

    change = RepoEnablement(repo_root).enable()
    if not change.git_hooks_resolved:
        # Fail loud, not silent: enable wrote nothing (the marker included), so
        # do NOT claim success. Emit the same NOTICE the install path uses and
        # exit non-zero. Mirrors the MCP `/biff enable` surface verbatim.
        print(HOOKS_DIR_UNRESOLVED_NOTICE)
        raise typer.Exit(code=1)
    if change.ci_workflow_changed:
        print("CI workflow: .github/workflows/biff-notify.yml")
    if change.git_hooks_changed:
        print(f"Git hooks: {', '.join(change.git_hooks_changed)}")

    print(
        "biff enabled. Commit .punt-labs/biff/enabled and "
        ".github/workflows/biff-notify.yml, then restart Claude Code "
        "for changes to take effect."
    )


@app.command()
def disable(
    start: Annotated[
        Path | None,
        typer.Option(help="Repo root (default: auto-detect)."),
    ] = None,
) -> None:
    """Disable biff in the current git repo — deactivates this clone.

    Removes exactly what ``enable`` added: the committed marker
    ``.punt-labs/biff/enabled`` and CI workflow
    ``.github/workflows/biff-notify.yml``, AND this clone's local
    ``.git/hooks`` biff dispatchers.  Equivalent to the MCP ``/biff disable``
    toggle.  Idempotent.  Commit the removal of the tracked files via a PR
    for it to take effect for every contributor.  Invokes git only read-only
    (to resolve the hooks directory) and never creates commits.
    """
    repo_root = find_git_root(start)
    if repo_root is None:
        raise SystemExit("Not in a git repository. Run this from inside a repo.")

    from biff.enablement import RepoEnablement

    change = RepoEnablement(repo_root).disable()
    if change.ci_workflow_changed:
        print("CI workflow removed: biff-notify.yml")
    if change.git_hooks_changed:
        print(f"Git hooks removed: {', '.join(change.git_hooks_changed)}")

    print(
        "biff disabled. Commit the removal of .punt-labs/biff/enabled and "
        ".github/workflows/biff-notify.yml, then restart Claude Code "
        "for changes to take effect."
    )


_PLUGIN_ID = "biff@punt-labs"


def _register_user_scope() -> None:
    """Deposit the agent guide and register the user-scope ``@``-import.

    Global-tool install (tool-enable-disable.md §2.6): guidance is universal, so
    biff registers ``@~/.punt-labs/biff/CLAUDE.md`` in ``~/.claude/CLAUDE.md``
    once at install rather than per repo.
    """
    from biff.user_scope import USER_IMPORT_LINE, UserScope

    result = UserScope().install()
    if result.guide_written:
        print("Agent guide: ~/.punt-labs/biff/CLAUDE.md")
    if result.import_registered:
        print(f"Registered {USER_IMPORT_LINE} in ~/.claude/CLAUDE.md")


def _deploy_repo_git_hooks() -> None:
    """Deploy biff's git hooks into the current clone's ``.git/hooks/``.

    Hooks are per-clone, local machinery — never committed (DES-052 rule 3).
    Every clone deploys its own, so hook deployment lives at ``install`` (a
    local, per-clone action), not at the committed-policy ``enable`` toggle.
    They gate on the enablement marker at runtime, so deploying them in a
    not-yet-enabled repo is a safe no-op until ``enable`` writes the marker.
    Skips silently outside a git repo — ``install`` is also a global action.
    """
    from biff.git_hooks import (
        HOOKS_DIR_UNRESOLVED_NOTICE,
        deploy_git_hooks,
        resolve_hooks_dir,
    )

    repo_root = find_git_root()
    if repo_root is None:
        return
    if resolve_hooks_dir(repo_root) is None:
        # Never a silent skip: a worktree/submodule or a missing git binary can
        # leave no hooks dir to write to. Emit the shared NOTICE rather than
        # reporting success while deploying nothing.
        print(HOOKS_DIR_UNRESOLVED_NOTICE)
        return
    hooks = deploy_git_hooks(repo_root)
    if hooks:
        print(f"Git hooks: {', '.join(hooks)}")


def _remove_repo_git_hooks() -> None:
    """Remove biff's git hooks from the current clone's ``.git/hooks/``.

    The per-clone counterpart to :func:`_deploy_repo_git_hooks`.  Skips
    silently outside a git repo.
    """
    from biff.git_hooks import remove_git_hooks

    repo_root = find_git_root()
    if repo_root is None:
        return
    hooks = remove_git_hooks(repo_root)
    if hooks:
        print(f"Git hooks removed: {', '.join(hooks)}")


@app.command("install")
def install_cmd() -> None:
    """Install biff via the punt-labs marketplace and this clone's git hooks."""
    import shutil
    import subprocess

    # User-scope guidance first — it needs no marketplace and leaves no
    # dangling import (the guide is deposited before the line is registered).
    _register_user_scope()

    # Per-clone git hooks (local, never committed). Safe no-op outside a repo.
    _deploy_repo_git_hooks()

    claude = shutil.which("claude")
    if not claude:
        # CLI-only install is a SUCCESS, not a partial failure: the biff CLI,
        # MCP server, and user-scope guide are all installed; only the Claude
        # Code plugin is skipped (install-cli-only.md, matching install.sh's
        # --no-plugin path). No "restart Claude Code" line — there's no plugin.
        print("Installed (CLI-only). Claude Code not found; plugin step skipped.")
        return

    result = subprocess.run(  # noqa: S603
        [claude, "plugin", "install", _PLUGIN_ID, "--scope", "user"],
        check=False,
    )
    if result.returncode != 0:
        raise typer.Exit(code=1)
    print("Installed. Restart Claude Code to activate.")


@app.command()
def doctor() -> None:
    """Check biff installation health."""
    from biff.doctor import check_environment

    code = check_environment()
    if code != 0:
        raise typer.Exit(code=code)


@app.command("uninstall")
def uninstall_cmd() -> None:
    """Uninstall biff: remove the plugin, this clone's git hooks, and the import."""
    import shutil
    import subprocess

    from biff.user_scope import USER_IMPORT_LINE, UserScope

    plugin_failed = False
    claude = shutil.which("claude")
    if claude:
        result = subprocess.run(  # noqa: S603
            [claude, "plugin", "uninstall", _PLUGIN_ID, "--scope", "user"],
            check=False,
        )
        plugin_failed = result.returncode != 0
    else:
        print("claude CLI not found; skipping plugin uninstall.")

    # Per-clone git hooks (deployed by `biff install` or `biff enable`) are
    # local machinery, so uninstall removes them for this clone. The committed
    # marker/CI workflow are repo policy — left untouched; `biff disable` owns
    # those (it also removes this clone's hooks).
    _remove_repo_git_hooks()

    # User-scope teardown (§2.6) ALWAYS runs — never gated on the plugin step.
    # The user asked to clean up, and a dangling @-import 404s every session, so
    # a failed or skipped plugin uninstall must not strand the import line. The
    # deposited guide stays dormant (§2.9).
    if UserScope().uninstall():
        print(f"Removed {USER_IMPORT_LINE} from ~/.claude/CLAUDE.md")

    if plugin_failed:
        # Surface the plugin failure — but only after the cleanup above ran.
        print("Warning: 'claude plugin uninstall' failed; user-scope import removed.")
        raise typer.Exit(code=1)
    if claude is None:
        # Mirror install's CLI-only messaging: never imply the plugin was
        # removed when there was no `claude` to remove it.
        print("Uninstalled (CLI-only). Claude Code not found; plugin was not removed.")
    else:
        print("Uninstalled.")


@app.command()
def statusline() -> None:
    """Output status bar text (called by Claude Code)."""
    from biff.statusline import run_statusline

    print(run_statusline())


# ---------------------------------------------------------------------------
# Talk — interactive conversation (uses shared session lifecycle)
# ---------------------------------------------------------------------------


@app.command()
def talk(
    to: Annotated[
        str,
        typer.Argument(help="User to talk to, e.g. jmf-pobox"),
    ],
    message: Annotated[
        str,
        typer.Argument(help="Opening message (optional)."),
    ] = "",
) -> None:
    """Start an interactive talk session with a teammate or agent.

    Opens a real-time conversation loop: type a message and press
    Enter to send, then wait for a reply.  Ctrl+C to end.

    This is the phone/terminal use case — steer an agent session
    from any device that can run ``biff talk``.
    """
    asyncio.run(_talk_interactive(to, message))


async def _talk_fetch_and_print(relay: object, session_key: str, user: str) -> None:
    """Fetch and print any unread messages using shared formatting."""
    from biff.server.tools.talk import fetch_all_unread, format_talk_messages

    if not isinstance(relay, NatsRelay):
        return
    messages = await fetch_all_unread(relay, session_key, user)
    if messages:
        print(format_talk_messages(messages))


def _stdin_reader(
    input_queue: queue_mod.Queue[str | None], stop: threading_mod.Event
) -> None:
    """Read lines from stdin in a dedicated thread."""
    while not stop.is_set():
        try:
            line = input("you> ")
        except EOFError:
            input_queue.put(None)
            return
        input_queue.put(line)


_NO_INPUT = object()


async def _wait_for_input_or_notify(
    aqueue: asyncio.Queue[str | None],
    notify_event: asyncio.Event,
) -> str | None | object:
    """Wait for user input, a NATS notification, or a 2s timeout."""
    input_task = asyncio.create_task(aqueue.get())
    notify_task = asyncio.create_task(notify_event.wait())

    done, pending = await asyncio.wait(
        {input_task, notify_task},
        return_when=asyncio.FIRST_COMPLETED,
        timeout=2.0,
    )
    for p in pending:
        p.cancel()
        with suppress(asyncio.CancelledError):
            await p

    if input_task in done:
        return input_task.result()
    return _NO_INPUT


async def _bridge_stdin(
    input_queue: queue_mod.Queue[str | None],
    aqueue: asyncio.Queue[str | None],
) -> None:
    """Bridge a threading.Queue to an asyncio.Queue via a single executor thread."""
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, input_queue.get)
        await aqueue.put(line)
        if line is None:
            break


async def _talk_loop(
    relay: object,
    session_key: str,
    user: str,
    target: str,
    *,
    target_repo: str | None = None,
    tty_name: str = "",
) -> None:
    """Set up the stdin bridge and notify SUB, then run the conversation loop."""
    if not isinstance(relay, NatsRelay):
        return

    input_queue: queue_mod.Queue[str | None] = queue_mod.Queue()
    stop_flag = threading_mod.Event()
    threading_mod.Thread(
        target=_stdin_reader, args=(input_queue, stop_flag), daemon=True
    ).start()

    aqueue: asyncio.Queue[str | None] = asyncio.Queue()
    bridge_task = asyncio.create_task(_bridge_stdin(input_queue, aqueue))
    notify_event = asyncio.Event()

    sub = _TalkSubscription(relay, session_key, notify_event)
    await sub.establish()
    try:
        await _talk_converse(
            relay,
            sub,
            aqueue,
            notify_event,
            session_key,
            user,
            target,
            target_repo=target_repo,
            tty_name=tty_name,
        )
    finally:
        stop_flag.set()
        bridge_task.cancel()
        with suppress(asyncio.CancelledError):
            await bridge_task
        await sub.close()


async def _talk_converse(
    relay: NatsRelay,
    sub: _TalkSubscription,
    aqueue: asyncio.Queue[str | None],
    notify_event: asyncio.Event,
    session_key: str,
    user: str,
    target: str,
    *,
    target_repo: str | None = None,
    tty_name: str = "",
) -> None:
    """Print incoming messages and send typed lines until EOF.

    A wedge teardown that swaps the NATS client mid-session would orphan the
    notify SUB on the dead client and silently stop incoming partner messages
    (sends still work — they redial through the relay).  Reconciling the SUB on
    each idle tick re-binds it regardless (nats-relay.tex talkSubGen); the call
    is crash-safe via the latch and a no-op when the generation is unchanged.

    The same client swap can make the per-tick durable fetch raise mid-redial.
    That call is guarded so a transient error is absorbed and the loop paces on
    through the wait below — the inbox is re-fetched next tick — rather than
    letting the traceback exit the whole ``biff talk`` command.  A fetch-scoped
    latch keeps the onset discipline: DEBUG per tick, one WARNING when the
    failure persists.  Its wording names the inbox-read cause, not a
    re-subscribe failure — the SUB may be healthy while the durable inbox is
    unreadable, and conflating the two would misdirect debugging.
    """
    from biff.models import Message

    fetch_latch = TalkNotifyLatch.for_fetch(logging.getLogger(__name__))
    while True:
        try:
            await _talk_fetch_and_print(relay, session_key, user)
        except (NatsError, TimeoutError, OSError):
            fetch_latch.record_failure()
        else:
            fetch_latch.record_success()
        notify_event.clear()

        result = await _wait_for_input_or_notify(aqueue, notify_event)
        if result is _NO_INPUT:
            await sub.reconcile()
            continue
        if not isinstance(result, str):
            break  # EOF (None) or unexpected type
        line = result.strip()
        if line:
            msg = Message(
                from_user=user,
                from_tty=tty_name,
                to_user=target,
                body=line[:512],
            )
            await relay.deliver(msg, sender_key=session_key, target_repo=target_repo)


async def _talk_interactive(to: str, opening: str) -> None:
    """Interactive talk loop using the shared CLI session lifecycle."""
    from biff.models import Message
    from biff.server.tools._session import resolve_talk_target
    from biff.tty import build_session_key, parse_address

    user_target, tty_target = parse_address(to)

    try:
        async with cli_session(interactive=True, user_override=_user_override) as ctx:
            if not isinstance(ctx.relay, NatsRelay):
                print("Talk requires a NATS relay.")
                return

            all_sessions = await ctx.relay.get_sessions_for_repos(ctx.visible_repos)
            sessions = [s for s in all_sessions if s.user == user_target]
            if not sessions:
                print(f"{user_target} is not online.")
                return

            # Talk is session-scoped: the address must name a session.  Talk
            # frames route on (org, identity); this standalone command instead
            # carries lines over the durable inbox (interactive write), whose
            # subject is repo-keyed (DES-030), so it resolves the peer's repo
            # for cross-repo delivery separately from talk routing.
            try:
                target, display = resolve_talk_target(
                    all_sessions,
                    user_target,
                    tty_target,
                    sender_key=ctx.session_key,
                    sender_repo=ctx.config.repo_name,
                )
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                raise typer.Exit(code=1) from None
            peer = next(
                (s for s in all_sessions if build_session_key(s.user, s.tty) == target),
                None,
            )
            target_repo = (
                peer.repo
                if peer and peer.repo and peer.repo != ctx.config.repo_name
                else None
            )

            # Update plan to show talk activity.
            await update_current_session(ctx, plan=f"talking to {display}")

            if opening:
                body = opening[:512]
                msg = Message(
                    from_user=ctx.user,
                    from_tty=ctx.tty_name,
                    to_user=target,
                    body=body,
                )
                await ctx.relay.deliver(
                    msg, sender_key=ctx.session_key, target_repo=target_repo
                )
                print(f"you> {body}")

            print(f"Connected to {display}. Type and press Enter. Ctrl+C to end.\n")

            await _talk_loop(
                ctx.relay,
                ctx.session_key,
                ctx.user,
                target,
                target_repo=target_repo,
                tty_name=ctx.tty_name,
            )
    except KeyboardInterrupt:
        print("\nTalk session ended.")
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise typer.Exit(code=1) from None


if __name__ == "__main__":
    app()
