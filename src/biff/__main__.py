"""Biff CLI entry point.

Provides ``biff serve``, ``biff version``, ``biff enable``, ``biff disable``,
``biff install``, ``biff doctor``, ``biff uninstall``, ``biff hook``,
``biff talk``, and status line management.
"""

from __future__ import annotations

import asyncio
import queue as queue_mod
import threading as threading_mod
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import click
import typer

if TYPE_CHECKING:
    from nats.aio.client import Client as NatsClient

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

app = typer.Typer(help="Biff: the dog that barked when messages arrived.")
app.add_typer(hook_app, name="hook")


@app.command()
def version() -> None:
    """Print the biff version."""
    print(f"biff {pkg_version('punt-biff')}")


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
    transport: Annotated[
        str,
        typer.Option(
            help="Transport: 'stdio' or 'http'.",
            click_type=click.Choice(["stdio", "http"]),
        ),
    ] = "stdio",
    host: Annotated[
        str, typer.Option(help="HTTP host (http transport only).")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="HTTP port (http transport only).")] = 8419,
) -> None:
    """Start the biff MCP server."""
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
    mcp = create_server(state)

    if transport == "http":
        print(f"Starting biff MCP server on http://{host}:{port}")
        mcp.run(transport="http", host=host, port=port)
    else:
        mcp.run(transport="stdio")


@app.command("install-statusline")
def install_statusline() -> None:
    """Install biff into Claude Code's status bar."""
    from biff.statusline import install

    result = install()
    print(result.message)
    if not result.installed:
        raise typer.Exit(code=1)


@app.command("uninstall-statusline")
def uninstall_statusline() -> None:
    """Remove biff from Claude Code's status bar."""
    from biff.statusline import uninstall

    result = uninstall()
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


@app.command()
def install() -> None:
    """Install biff via the punt-labs marketplace."""
    from biff.installer import install as do_install

    result = do_install()
    for step in result.steps:
        symbol = "\u2713" if step.passed else "\u2717"
        print(f"  {symbol} {step.name}: {step.message}")
    print()
    print(result.message)
    if not result.installed:
        raise typer.Exit(code=1)


@app.command()
def doctor() -> None:
    """Check biff installation health."""
    from biff.doctor import check_environment

    code = check_environment()
    if code != 0:
        raise typer.Exit(code=code)


@app.command()
def uninstall() -> None:
    """Uninstall biff plugin and clean up artifacts."""
    from biff.installer import uninstall as do_uninstall

    result = do_uninstall()
    for step in result.steps:
        symbol = "\u2713" if step.passed else "\u2717"
        print(f"  {symbol} {step.name}: {step.message}")
    print()
    print(result.message)
    if not result.uninstalled:
        raise typer.Exit(code=1)


@app.command()
def statusline() -> None:
    """Output status bar text (called by Claude Code)."""
    from biff.statusline import run_statusline

    print(run_statusline())


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
    import asyncio as _asyncio

    _asyncio.run(_talk_repl(to, message))


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
    """Read lines from stdin in a dedicated thread.

    Runs until EOF or ``stop`` is set.  Each line is put into ``input_queue``
    as a string.  Sentinel ``None`` signals EOF.
    """
    while not stop.is_set():
        try:
            line = input("you> ")
        except EOFError:
            input_queue.put(None)
            return
        input_queue.put(line)


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

            loop = asyncio.get_running_loop()
            input_fut = asyncio.ensure_future(
                loop.run_in_executor(None, input_queue.get, True, 2.0)
            )
            notify_fut = asyncio.ensure_future(notify_event.wait())

            futs: set[asyncio.Future[object]] = {
                input_fut,  # type: ignore[arg-type]
                notify_fut,  # type: ignore[arg-type]
            }
            await asyncio.wait(futs, return_when=asyncio.FIRST_COMPLETED)

            if notify_fut.done():
                if not input_fut.done():
                    input_fut.cancel()
                    continue
                line = input_fut.result()
            elif input_fut.done():
                notify_fut.cancel()
                line = input_fut.result()
            else:
                # Both timed out.
                input_fut.cancel()
                notify_fut.cancel()
                continue

            if line is None:
                break  # EOF
            line = line.strip()
            if not line:
                continue
            msg = Message(from_user=user, to_user=target, body=line[:512])
            await relay.deliver(msg)
    finally:
        stop_flag.set()
        await sub.unsubscribe()


async def _talk_repl(to: str, opening: str) -> None:
    """Interactive talk loop: send messages, receive replies in real-time."""
    import sys

    from biff.config import load_config
    from biff.models import Message, UserSession
    from biff.nats_relay import NatsRelay
    from biff.tty import generate_tty, parse_address

    resolved = load_config()
    config = resolved.config
    if not config.relay_url:
        print("Talk requires a NATS relay. Configure relay_url in .biff.")
        sys.exit(1)

    user, _tty_target = parse_address(to)
    relay = NatsRelay(
        url=config.relay_url,
        auth=config.relay_auth,
        name=f"biff-talk-{config.user}",
        repo_name=config.repo_name,
    )
    tty = generate_tty()

    try:
        sessions = await relay.get_sessions_for_user(user)
        if not sessions:
            print(f"@{user} is not online.")
            return

        await relay.update_session(
            UserSession(
                user=config.user, tty=tty, tty_name="talk", plan=f"talking to @{user}"
            )
        )

        if opening:
            msg = Message(from_user=config.user, to_user=user, body=opening[:512])
            await relay.deliver(msg)
            print(f"you> {opening}")

        print(f"Connected to @{user}. Type a message and press Enter. Ctrl+C to end.\n")

        nc = await relay.get_nc()
        subject = relay.talk_notify_subject(config.user)
        session_key = f"{config.user}:{tty}"

        await _talk_loop(relay, nc, subject, session_key, config.user, user)

    except KeyboardInterrupt:
        print("\nTalk session ended.")
    finally:
        from contextlib import suppress

        from biff.tty import build_session_key

        with suppress(Exception):
            await relay.delete_session(build_session_key(config.user, tty))
        await relay.close()


if __name__ == "__main__":
    app()
