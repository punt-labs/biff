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
from typing import TYPE_CHECKING, Annotated

import typer
from nats.errors import Error as NatsError

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from nats.aio.client import Client as NatsClient

    from biff.server.state import ServerState

from biff import commands
from biff.cli_session import CliContext, cli_session
from biff.commands import CommandResult
from biff.config import (
    ensure_gitignore_yaml,
    find_git_root,
    is_enabled,
    load_mcp_config,
    write_yaml_local_enabled,
)
from biff.formatting import terminal_safe
from biff.hook import hook_app
from biff.nats_relay import NatsRelay
from biff.repl_display import ReplDisplay
from biff.server.app import create_server
from biff.server.state import create_state
from biff.talk_types import AcceptOutcome, TalkNotification

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
    REPL (biff-9la).  At INFO the transient events — all demoted to INFO in
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
    """Flush stdout, then open the prompt gate (biff-1xt5).

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
    """Handle the REPL-only ``timestamps on|off`` toggle (biff-4uq).

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
    # predate the display toggle — see ReplDisplay (biff-4uq).
    display: ReplDisplay | None = None,
) -> list[str]:
    """Format drained idle-mode notifications as REPL banner lines.

    Invites render as a phone banner; other bodied notifications as a
    ``▶`` line honouring the timestamp toggle.  Accepts are silent
    (the handshake owns them).  The pending-invite bookkeeping lives in
    :meth:`TalkState.drain_idle`; this is pure presentation.
    """
    lines: list[str] = []
    for notif in notifs:
        if notif.is_accept:
            continue
        sender = terminal_safe(notif.nfrom)
        body = terminal_safe(notif.nbody)
        if notif.is_invite and body:
            lines.append(f"  \033[1;33m📞 {sender}: {body}\033[0m")
        elif body:
            sender_tty = terminal_safe(notif.nfrom_tty)
            label = f"{sender}:{sender_tty}" if sender_tty else sender
            stamp = display.stamp(datetime.now(UTC)) if display is not None else ""
            lines.append(f"  \033[1;33m{stamp}{label} ▶ {body}\033[0m")
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
    # tests) that predate the display toggle — see ReplDisplay (biff-4uq).
    display: ReplDisplay | None = None,
) -> list[str]:
    """Format drained connected-mode notifications as conversation lines.

    Messages render as a cyan ``user:tty ▶ message`` line honouring the
    timestamp toggle; an end frame renders a dim hangup line.  Invites
    and accepts are already filtered out by :meth:`TalkState.drain_connected`.
    """
    lines: list[str] = []
    for notif in notifs:
        if notif.is_end:
            sender = terminal_safe(notif.nfrom)
            sender_tty = terminal_safe(notif.nfrom_tty)
            label = f"{sender}:{sender_tty}" if sender_tty else sender
            lines.append(f"\033[2m{label} has ended the conversation.\033[0m")
            continue
        body = terminal_safe(notif.nbody)
        if not body:
            continue
        sender = terminal_safe(notif.nfrom)
        sender_tty = terminal_safe(notif.nfrom_tty)
        label = f"{sender}:{sender_tty}" if sender_tty else sender
        stamp = display.stamp(datetime.now(UTC)) if display is not None else ""
        lines.append(f"\033[36m{stamp}{label} ▶ {body}\033[0m")
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


async def _repl_talk(
    ctx: CliContext,
    target_user: str,
    display: str,
    aqueue: asyncio.Queue[str | None],
    notify_event: asyncio.Event,
    prompt_gate: threading_mod.Event,
    current_prompt: list[str],
    repl_prompt: str,
    repl_display: ReplDisplay,
    *,
    to_key: str,
    target_repo: str | None = None,
) -> None:
    """Modal talk sub-loop — send lines to target, show incoming messages.

    Runs until the user types ``end`` or the input stream ends (EOF/Ctrl-C).
    Returns control to the REPL loop when done.  Swaps the prompt to
    a talk-specific one and restores the REPL prompt on exit.

    Messages are sent via the shared ``TalkState`` (ephemeral core-NATS
    publish, no inbox) and received by draining it each 2s tick.
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
                if _render_connected_drain(ctx, repl_display, talk_prompt):
                    break
                continue

            if result is None:
                break
            if not isinstance(result, str):
                break

            line = result.strip()
            if line.lower() == "end":
                await ctx.talk.send_end(
                    target_user=target_user, to_key=to_key, target_repo=target_repo
                )
                break

            if line:
                await ctx.talk.send_message(
                    target_user=target_user,
                    to_key=to_key,
                    body=line,
                    target_repo=target_repo,
                )

            _release_prompt(prompt_gate)
    finally:
        # Whatever exit path (end, EOF, remote hangup) — return to idle so
        # the REPL's idle drain renders correctly (talk.tex LocalEnd).
        ctx.talk.reset()
        current_prompt[0] = repl_prompt
        # Clear the talk plan when exiting talk mode.
        try:
            session = await ctx.relay.get_session(ctx.session_key)
            if session is not None:
                updated = session.model_copy(update={"plan": ""})
                await ctx.relay.update_session(updated)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).debug("Failed to clear talk plan")

    # Clear any stale prompt the stdin thread may have printed.
    print(f"\r\033[KTalk with {display} ended.")


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
            # Timeout or notification — check for changes inline.
            notify_event.clear()
            await _poll_notify(
                ctx,
                notify,
                prompt,
                inline=True,
                display=repl_display,
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
            )
            _release_prompt(prompt_gate)
            continue

        # REPL-only display toggle (not an MCP tool) — biff-4uq.
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
    """Print a third-party talk notification as a terminal-safe banner."""
    sender = terminal_safe(notif.nfrom)
    body = terminal_safe(notif.nbody)
    if body:
        print(f"\r\033[K  \033[1;33m📞 {sender}: {body}\033[0m")


async def _wait_for_talk_accept(
    ctx: CliContext,
    aqueue: asyncio.Queue[str | None],
    notify_event: asyncio.Event,
    prompt_gate: threading_mod.Event,
) -> AcceptOutcome:
    """Wait for the target to accept, or for a mutual-invite auto-accept.

    Returns the :class:`AcceptOutcome`; ``NONE`` when the user typed
    ``end`` or EOF before any accept arrived.  Third-party notifications
    surfaced by :meth:`TalkState.poll_accept` print as banners.
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


async def _withdraw_talk_invite(
    ctx: CliContext,
    target_user: str,
    target_key: str,
    *,
    target_repo: str | None = None,
) -> None:
    """Withdraw an outstanding outgoing invite and return to idle.

    Abandoning an invite publishes ``ntWithdraw`` so the invitee's ``[TALK]``
    marker clears at once (notification.tex ``WithdrawArrive``) instead of
    lingering until the TTL sweep, then resets to idle and clears the talk
    plan.  A connected hangup is a distinct path (``send_end`` / ``DrainEnd``).

    The local reset and plan-clear happen *regardless of* the publish: the
    withdraw is a best-effort core-NATS publish, so a wedged or reconnecting
    relay (including the Ctrl-C cancel path) must never strand the session in a
    phantom inviting state or leak a terminal traceback.  On a publish failure
    the invitee still clears via the pending-invite TTL sweep
    (notification.tex ``ExpirePendingInvite``).
    """
    ctx.talk.reset()
    try:
        await ctx.talk.send_withdraw(
            target_user=target_user, to_key=target_key, target_repo=target_repo
        )
    except (NatsError, TimeoutError, OSError):
        logging.getLogger(__name__).warning(
            "talk withdraw to %s failed; invitee falls back to the TTL sweep",
            target_user,
            exc_info=True,
        )
    try:
        s = await ctx.relay.get_session(ctx.session_key)
        if s is not None:
            await ctx.relay.update_session(s.model_copy(update={"plan": ""}))
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).debug("Failed to clear plan")


async def _talk_handshake(
    ctx: CliContext,
    target_user: str,
    target_key: str,
    display: str,
    args: list[str],
    responding: bool,
    aqueue: asyncio.Queue[str | None],
    notify_event: asyncio.Event,
    prompt_gate: threading_mod.Event,
    *,
    target_repo: str | None = None,
) -> bool:
    """Execute the talk handshake. Returns True if talk should proceed."""
    if responding:
        # We're accepting an existing invite. Send accept, enter talk.
        await ctx.talk.send_accept(
            target_user=target_user, to_key=target_key, target_repo=target_repo
        )
        return True

    # We're initiating. Send invite and wait for accept.
    invite_body = f"wants to talk — reply with: talk {ctx.user}:{ctx.tty_name}"
    if len(args) > 1:
        invite_body = " ".join(args[1:])[:512]

    await ctx.talk.send_invite(
        target_user=target_user,
        to_key=target_key,
        body=invite_body,
        target_repo=target_repo,
    )

    if len(args) > 1:
        print(f"you> {invite_body}")

    # Clear the stdin thread's prompt first so the line lands clean, not
    # appended to a stale ``user:tty ▶`` prompt (same pattern as :303/:311).
    print("\r\033[K", end="")
    print(f"Waiting for {display} to respond... (type 'end' to cancel)")

    # A Ctrl-C during the invite fires the withdraw, then exits the REPL to
    # the shell.  ``asyncio.run`` cancels the main task on SIGINT, so the wait
    # raises ``CancelledError`` — not ``KeyboardInterrupt``, which the runner
    # re-raises only after the task has unwound.  Catch the cancel, publish
    # ``ntWithdraw`` (notification.tex WithdrawArrive) so the invitee's
    # ``[TALK]`` marker clears at once rather than at the TTL sweep, then
    # re-raise so the cancellation propagates and the process exits normally.
    # ``end``/``exit``/``quit`` is the graceful in-REPL cancel that returns to
    # the prompt (``AcceptOutcome.NONE`` below); Ctrl-C is a process exit.
    try:
        outcome = await _wait_for_talk_accept(ctx, aqueue, notify_event, prompt_gate)
    except (asyncio.CancelledError, KeyboardInterrupt):
        await _withdraw_talk_invite(
            ctx, target_user, target_key, target_repo=target_repo
        )
        raise
    if outcome is AcceptOutcome.NONE:
        print(f"Talk with {display} cancelled.")
        await _withdraw_talk_invite(
            ctx, target_user, target_key, target_repo=target_repo
        )
        return False
    if outcome is AcceptOutcome.AUTO_ACCEPT:
        # Mutual invite: we are the higher key — accept the partner's invite
        # so their side stops waiting.  Deterministic, no deadlock (DES-043).
        await ctx.talk.send_accept(
            target_user=target_user, to_key=target_key, target_repo=target_repo
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
) -> None:
    """Parse talk args and enter modal talk mode."""
    from biff.server.tools._session import resolve_talk_target
    from biff.tty import parse_address

    if not args:
        print("Usage: talk @user:ttyN [message]")
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
    responding_key = ctx.talk.consume_pending_invite(user_target)
    responding = responding_key is not None
    resolve_user, resolve_tty = (user_target, tty_target)
    if responding_key is not None:
        resolve_user, _, resolve_tty = responding_key.partition(":")
    try:
        target_key, display, target_repo = resolve_talk_target(
            all_sessions,
            resolve_user,
            resolve_tty,
            sender_key=ctx.session_key,
            sender_repo=ctx.config.repo_name,
        )
    except ValueError as exc:
        print(f"Error: {exc}")
        return

    # Enter the appropriate phase before the handshake so the accept poll
    # and connected drain see the partner key (talk.tex SendInvite /
    # RespondToInvite).  ``resolve_tty`` is the partner tty (address for an
    # initiator, the invite's session for a responder).
    if responding:
        ctx.talk.begin_connected(
            partner=user_target, partner_tty=resolve_tty or "", partner_key=target_key
        )
    else:
        ctx.talk.begin_invite(
            partner=user_target, partner_tty=resolve_tty or "", partner_key=target_key
        )

    # Update plan to show talk activity.
    session = await ctx.relay.get_session(ctx.session_key)
    if session is not None:
        updated = session.model_copy(update={"plan": f"talking to {display}"})
        await ctx.relay.update_session(updated)

    if not await _talk_handshake(
        ctx,
        user_target,
        target_key,
        display,
        args,
        responding,
        aqueue,
        notify_event,
        prompt_gate,
        target_repo=target_repo,
    ):
        return

    await _repl_talk(
        ctx,
        user_target,
        display,
        aqueue,
        notify_event,
        prompt_gate,
        current_prompt,
        repl_prompt,
        repl_display,
        to_key=target_key,
        target_repo=target_repo,
    )


async def _setup_nats_subscription(
    ctx: CliContext,
    notify_event: asyncio.Event,
) -> object | None:
    """Subscribe to NATS talk notifications, feeding the shared ``TalkState``.

    Always-on for the REPL lifetime: every talk frame flows into
    ``ctx.talk.receive`` (self-echo and session-scope filtering happen
    there).  Returns the subscription object (for cleanup) or ``None``
    if the relay is not NATS-backed.
    """
    if not isinstance(ctx.relay, NatsRelay):
        return None

    nc = await ctx.relay.get_nc()
    subject = ctx.relay.talk_notify_subject(ctx.user)

    async def _on_notify(msg: object) -> None:
        data = getattr(msg, "data", b"")
        if data and data != b"1":
            try:
                raw: object = json.loads(data)
                if isinstance(raw, dict):
                    notification: dict[str, str] = {
                        str(k): str(v)  # pyright: ignore[reportUnknownArgumentType]
                        for k, v in raw.items()  # pyright: ignore[reportUnknownVariableType,reportUnknownMemberType]
                    }
                    ctx.talk.receive(notification)
            except (json.JSONDecodeError, TypeError):
                logging.getLogger(__name__).debug(
                    "Failed to process talk notification", exc_info=True
                )
        notify_event.set()

    return await nc.subscribe(  # pyright: ignore[reportUnknownMemberType]
        subject, cb=_on_notify
    )


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
            sub = await _setup_nats_subscription(ctx, notify_event)

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
                if sub is not None:
                    with suppress(Exception):
                        await sub.unsubscribe()  # type: ignore[attr-defined]
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
    user: Annotated[str, typer.Argument(help="User to query, e.g. @kai or @kai:tty1")],
) -> None:
    """Check what a user is working on and their availability."""
    _run(lambda ctx: commands.finger(ctx, user))


@app.command("write")
def write_cmd(
    to: Annotated[str, typer.Argument(help="Recipient, e.g. @kai or @kai:tty1")],
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
    from biff.session_key import find_session_key
    from biff.statusline import UNREAD_DIR

    resolved = load_mcp_config(
        user_override=user,
        data_dir_override=data_dir,
        relay_url_override=relay_url if relay_url is not None else RELAY_URL_UNSET,
        prefix=prefix,
    )
    dormant = not is_enabled(resolved.repo_root)

    # Companion (human) registration is deferred to the heartbeat
    # loop -- the ethos roster is not yet available at startup on
    # claude --resume (spec § 3.2, biff-8fg3).
    state = create_state(
        resolved.config,
        resolved.data_dir,
        unread_path=UNREAD_DIR / f"{find_session_key()}.json",
        dormant=dormant,
        repo_root=resolved.repo_root,
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
    """Enable biff in the current git repo.

    Writes ``config.local.yaml`` with ``enabled: true``, ensures it
    is gitignored, deploys git hooks and CI workflow.  Matches the
    MCP ``/biff y`` toggle — no interactive prompts, no
    ``config.yaml`` creation.  Idempotent.
    """
    repo_root = find_git_root(start)
    if repo_root is None:
        raise SystemExit("Not in a git repository. Run this from inside a repo.")

    write_yaml_local_enabled(repo_root, enabled=True)
    ensure_gitignore_yaml(repo_root)

    from biff.ci_workflow import deploy_ci_workflow
    from biff.git_hooks import deploy_git_hooks

    hooks = deploy_git_hooks(repo_root)
    if hooks:
        print(f"Git hooks: {', '.join(hooks)}")

    if deploy_ci_workflow(repo_root):
        print("CI workflow: .github/workflows/biff-notify.yml")

    print("biff enabled. Restart Claude Code for changes to take effect.")


@app.command()
def disable(
    start: Annotated[
        Path | None,
        typer.Option(help="Repo root (default: auto-detect)."),
    ] = None,
) -> None:
    """Disable biff in the current git repo.

    Writes ``config.local.yaml`` with ``enabled: false``.  Idempotent.
    """
    repo_root = find_git_root(start)
    if repo_root is None:
        raise SystemExit("Not in a git repository. Run this from inside a repo.")

    write_yaml_local_enabled(repo_root, enabled=False)
    ensure_gitignore_yaml(repo_root)

    from biff.ci_workflow import remove_ci_workflow
    from biff.git_hooks import remove_git_hooks

    hooks = remove_git_hooks(repo_root)
    if hooks:
        print(f"Git hooks removed: {', '.join(hooks)}")

    if remove_ci_workflow(repo_root):
        print("CI workflow removed: biff-notify.yml")

    print("biff disabled. Restart Claude Code for changes to take effect.")


_PLUGIN_ID = "biff@punt-labs"


@app.command("install")
def install_cmd() -> None:
    """Install biff via the punt-labs marketplace."""
    import shutil
    import subprocess

    claude = shutil.which("claude")
    if not claude:
        print("Error: claude CLI not found on PATH")
        raise typer.Exit(code=1)

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
    """Uninstall biff plugin and clean up artifacts."""
    import shutil
    import subprocess

    claude = shutil.which("claude")
    if not claude:
        print("Error: claude CLI not found on PATH")
        raise typer.Exit(code=1)

    result = subprocess.run(  # noqa: S603
        [claude, "plugin", "uninstall", _PLUGIN_ID, "--scope", "user"],
        check=False,
    )
    if result.returncode != 0:
        raise typer.Exit(code=1)
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
        typer.Argument(help="User to talk to, e.g. @jmf-pobox"),
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
    nc: NatsClient,
    subject: str,
    session_key: str,
    user: str,
    target: str,
    *,
    target_repo: str | None = None,
    tty_name: str = "",
) -> None:
    """Run the talk conversation loop with notification-driven message display."""
    from biff.models import Message

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

    async def _on_notify(_msg: object) -> None:
        notify_event.set()

    sub = await nc.subscribe(  # pyright: ignore[reportUnknownMemberType]
        subject, cb=_on_notify
    )
    try:
        while True:
            await _talk_fetch_and_print(relay, session_key, user)
            notify_event.clear()

            result = await _wait_for_input_or_notify(aqueue, notify_event)
            if result is _NO_INPUT:
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
                await relay.deliver(
                    msg, sender_key=session_key, target_repo=target_repo
                )
    finally:
        stop_flag.set()
        bridge_task.cancel()
        with suppress(asyncio.CancelledError):
            await bridge_task
        with suppress(Exception):
            await sub.unsubscribe()


async def _talk_interactive(to: str, opening: str) -> None:
    """Interactive talk loop using the shared CLI session lifecycle."""
    from biff.models import Message
    from biff.server.tools._session import resolve_talk_target
    from biff.tty import parse_address

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

            # Talk is session-scoped: the address must name a session.
            try:
                target, display, target_repo = resolve_talk_target(
                    all_sessions,
                    user_target,
                    tty_target,
                    sender_key=ctx.session_key,
                    sender_repo=ctx.config.repo_name,
                )
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                raise typer.Exit(code=1) from None

            # Update plan to show talk activity.
            session = await ctx.relay.get_session(ctx.session_key)
            if session is not None:
                updated = session.model_copy(update={"plan": f"talking to {display}"})
                await ctx.relay.update_session(updated)

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

            nc = await ctx.relay.get_nc()
            subject = ctx.relay.talk_notify_subject(ctx.user)

            await _talk_loop(
                ctx.relay,
                nc,
                subject,
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
