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
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from nats.aio.client import Client as NatsClient

    from biff.server.state import ServerState

from biff import commands
from biff.cli_session import CliContext, cli_session
from biff.commands import CommandResult
from biff.config import (
    DEMO_RELAY_URL,
    build_biff_toml,
    ensure_gitignore,
    find_git_root,
    get_github_identity,
    get_os_user,
    is_enabled,
    load_config,
    write_biff_local,
)
from biff.hook import hook_app
from biff.server.app import create_server
from biff.server.state import create_state

# ---------------------------------------------------------------------------
# Global flags
#
# Global flags (--json, --verbose, --quiet) go before the subcommand,
# following beads convention: ``biff --json who``, not ``biff who --json``.
# ---------------------------------------------------------------------------

_json_output = False
_quiet_output = False


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
    """Suppress nats.py noise common to all CLI invocations."""
    warnings.filterwarnings("ignore", category=DeprecationWarning, module="nats")
    logging.getLogger("biff.nats_relay").setLevel(logging.ERROR)
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
) -> None:
    """Biff: team communication for software engineers."""
    if verbose and quiet:
        raise typer.BadParameter("--verbose and --quiet are mutually exclusive.")

    global _json_output, _quiet_output
    _json_output = json_flag
    _quiet_output = quiet

    if verbose:
        logger = logging.getLogger("biff")
        logger.setLevel(logging.DEBUG)
        has_stderr = any(
            isinstance(h, logging.StreamHandler)
            and getattr(h, "stream", None) is sys.stderr  # pyright: ignore[reportUnknownArgumentType]
            for h in logger.handlers
        )
        if not has_stderr:
            handler = logging.StreamHandler(sys.stderr)
            handler.setLevel(logging.DEBUG)
            logger.addHandler(handler)

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


async def _poll_notify(
    ctx: CliContext, notify: object, prompt: str, *, inline: bool = False
) -> None:
    """Check for notification changes and print if any."""
    from biff.repl_notify import NotifyState

    if not isinstance(notify, NotifyState):
        return
    _log = logging.getLogger(__name__)
    try:
        summary = await ctx.relay.get_unread_summary(ctx.session_key)
        wall_post = await ctx.relay.get_wall()
        notes = notify.check(summary.count, wall_post)
        if notes and inline:
            # Clear current input line, print notifications, reshow prompt.
            print("\r\033[K", end="")
            for note in notes:
                print(note)
            print(prompt, end="", flush=True)
        elif notes:
            for note in notes:
                print(note)
    except Exception:  # noqa: BLE001
        _log.debug("Notify check failed", exc_info=True)


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


async def _repl_loop(
    ctx: CliContext,
    notify: object,
    prompt: str,
    aqueue: asyncio.Queue[str | None],
    notify_event: asyncio.Event,
    prompt_gate: threading_mod.Event,
) -> None:
    """Core REPL input loop — dispatches commands and handles notifications."""
    from biff.dispatch import dispatch

    while True:
        result = await _wait_for_input_or_notify(aqueue, notify_event)
        if result is _NO_INPUT:
            # Timeout or notification — check for changes inline.
            notify_event.clear()
            await _poll_notify(ctx, notify, prompt, inline=True)
            continue

        if result is None:
            print()
            break
        if not isinstance(result, str):
            break

        line = result

        try:
            cmd_result = await dispatch(line, ctx)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            prompt_gate.set()
            continue

        if cmd_result is None:
            break
        if cmd_result.text:
            print(cmd_result.text)

        # Sync state after the user's own command so the next poll
        # doesn't notify about changes the user just made.
        await _sync_notify(ctx, notify)

        # Allow the stdin thread to print the next prompt.
        prompt_gate.set()


async def _repl() -> None:
    """Interactive REPL: connect once, run commands, clean up on exit.

    Uses a stdin reader thread so the event loop stays unblocked —
    heartbeat and notification polling run while the user is idle at
    the prompt.  Message notifications are NATS-driven (instant);
    wall changes are detected via 2s timeout polling.

    Readline provides line editing (arrow keys), command history
    (up/down, persisted to ``~/.biff/repl_history``), and tab
    completion for command names.
    """
    from biff.dispatch import available_commands
    from biff.nats_relay import NatsRelay
    from biff.repl_notify import NotifyState
    from biff.repl_readline import setup as setup_readline

    cmds = available_commands()
    setup_readline(cmds)

    try:
        async with cli_session(interactive=True) as ctx:
            print(f"biff {pkg_version('punt-biff')} — {ctx.user}:{ctx.tty_name}")
            print(f"Commands: {', '.join(cmds)}, exit")
            print()

            notify = NotifyState()
            prompt = f"{ctx.user}:{ctx.tty_name}> "

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
                        ln = input(prompt)
                    except (EOFError, KeyboardInterrupt):
                        input_queue.put(None)
                        return
                    input_queue.put(ln)

            threading_mod.Thread(target=_read_stdin, daemon=True).start()
            aqueue: asyncio.Queue[str | None] = asyncio.Queue()
            bridge_task = asyncio.create_task(_bridge_stdin(input_queue, aqueue))

            # Subscribe to NATS notifications for real-time alerts.
            notify_event = asyncio.Event()
            sub = None
            if isinstance(ctx.relay, NatsRelay):
                nc = await ctx.relay.get_nc()
                subject = ctx.relay.talk_notify_subject(ctx.user)

                async def _on_notify(_msg: object) -> None:
                    notify_event.set()

                sub = await nc.subscribe(  # pyright: ignore[reportUnknownMemberType]
                    subject, cb=_on_notify
                )

            try:
                await _repl_loop(ctx, notify, prompt, aqueue, notify_event, prompt_gate)
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
                        await sub.unsubscribe()
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
            async with cli_session() as ctx:
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
    message: Annotated[str, typer.Argument(help="Message to send (max 512 chars)")],
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

    resolved = load_config(
        user_override=user,
        data_dir_override=data_dir,
        relay_url_override=relay_url if relay_url is not None else RELAY_URL_UNSET,
        prefix=prefix,
    )
    dormant = not is_enabled(resolved.repo_root)
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
        user=user,
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
        user=user,
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

    If no ``.biff`` team config exists, runs the interactive init flow
    (identity resolution, team members, relay URL) to create one.
    Writes ``.biff.local`` with ``enabled = true`` and ensures it is
    gitignored.  Idempotent — safe to run multiple times.
    """
    repo_root = find_git_root(start)
    if repo_root is None:
        raise SystemExit("Not in a git repository. Run this from inside a repo.")

    biff_file = repo_root / ".biff"
    if not biff_file.exists():
        # Interactive init flow — create .biff
        identity = get_github_identity()
        user = (identity.login if identity is not None else None) or get_os_user()
        if user is None:
            raise SystemExit(
                "Could not determine username.\n"
                "Install the gh CLI and authenticate: gh auth login"
            )
        print(f"Identity: {user}")

        members_input = typer.prompt(
            "Team members (comma-separated, or empty)",
            default="",
            show_default=False,
        )
        members = [m.strip() for m in members_input.split(",") if m.strip()]

        relay_url = typer.prompt(
            "Relay URL",
            default=DEMO_RELAY_URL,
        )

        biff_file.write_text(build_biff_toml(members, relay_url))
        print(f"Created {biff_file}")
        if members:
            print(f"  Team: {', '.join(members)}")
        if relay_url:
            print(f"  Relay: {relay_url}")

    write_biff_local(repo_root, enabled=True)
    ensure_gitignore(repo_root)

    from biff.git_hooks import deploy_git_hooks

    hooks = deploy_git_hooks(repo_root)
    if hooks:
        print(f"Git hooks: {', '.join(hooks)}")

    print("biff enabled. Restart Claude Code for changes to take effect.")


@app.command()
def disable(
    start: Annotated[
        Path | None,
        typer.Option(help="Repo root (default: auto-detect)."),
    ] = None,
) -> None:
    """Disable biff in the current git repo.

    Writes ``.biff.local`` with ``enabled = false``.  Idempotent.
    """
    repo_root = find_git_root(start)
    if repo_root is None:
        raise SystemExit("Not in a git repository. Run this from inside a repo.")

    write_biff_local(repo_root, enabled=False)
    ensure_gitignore(repo_root)

    from biff.git_hooks import remove_git_hooks

    hooks = remove_git_hooks(repo_root)
    if hooks:
        print(f"Git hooks removed: {', '.join(hooks)}")

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
    from biff.nats_relay import NatsRelay
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
) -> None:
    """Run the talk conversation loop with notification-driven message display."""
    from biff.models import Message
    from biff.nats_relay import NatsRelay

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
                msg = Message(from_user=user, to_user=target, body=line[:512])
                await relay.deliver(msg, sender_key=session_key)
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
    from biff.nats_relay import NatsRelay
    from biff.server.tools._session import resolve_session
    from biff.tty import build_session_key, parse_address

    user_target, tty_target = parse_address(to)
    display = f"@{user_target}:{tty_target}" if tty_target else f"@{user_target}"

    try:
        async with cli_session(interactive=True) as ctx:
            if not isinstance(ctx.relay, NatsRelay):
                print("Talk requires a NATS relay.")
                return

            sessions = await ctx.relay.get_sessions_for_user(user_target)
            if not sessions:
                print(f"@{user_target} is not online.")
                return

            # Resolve :tty suffix to a relay key (hex ID or tty_name match).
            if tty_target:
                resolved = await resolve_session(ctx.relay, user_target, tty_target)
                if resolved:
                    target = build_session_key(resolved.user, resolved.tty)
                else:
                    target = f"{user_target}:{tty_target}"
            else:
                target = user_target

            # Update plan to show talk activity.
            session = await ctx.relay.get_session(ctx.session_key)
            if session is not None:
                updated = session.model_copy(update={"plan": f"talking to {display}"})
                await ctx.relay.update_session(updated)

            if opening:
                body = opening[:512]
                msg = Message(from_user=ctx.user, to_user=target, body=body)
                await ctx.relay.deliver(msg, sender_key=ctx.session_key)
                print(f"you> {body}")

            print(f"Connected to {display}. Type and press Enter. Ctrl+C to end.\n")

            nc = await ctx.relay.get_nc()
            subject = ctx.relay.talk_notify_subject(ctx.user)

            await _talk_loop(ctx.relay, nc, subject, ctx.session_key, ctx.user, target)
    except KeyboardInterrupt:
        print("\nTalk session ended.")
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise typer.Exit(code=1) from None


if __name__ == "__main__":
    app()
