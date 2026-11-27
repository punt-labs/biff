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
import os
import sys
import warnings
from collections.abc import Awaitable, Callable
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Annotated

import typer

from biff._cli_runtime import CliInvocation
from biff.cli_app import CliApp
from biff.cli_daemon import Answer, DaemonCommand, Delivery, ResultPrinter
from biff.cli_direct_wall import DirectWall
from biff.cli_key import KeyCli
from biff.cli_repl import ReplSession
from biff.cli_talk import TalkConversation
from biff.cli_watch import WatchCommand
from biff.client.environment import ClientEnvironment
from biff.config import find_git_root
from biff.doctor import installed_plugin_version

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global flags
#
# Global flags (--json, --verbose, --quiet) go before the subcommand,
# following beads convention: ``biff --json who``, not ``biff who --json``.
# ---------------------------------------------------------------------------

_json_output = False
_quiet_output = False
_user_override: str | None = None
_session_ref: str | None = None


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
    """Suppress upstream deprecation and known disconnect warnings."""
    warnings.filterwarnings("ignore", category=DeprecationWarning, module="nats")
    _install_eof_received_filter()


app = CliApp.build()
session_app = typer.Typer(help="Create, list, select and end named CLI sessions.")
app.add_typer(session_app, name="session")


def _versions() -> tuple[str, str | None]:
    """Return (PyPI package version, plugin version or None if no plugin)."""
    return pkg_version("punt-biff"), installed_plugin_version()


def _version_line(ver: str, plugin: str | None) -> str:
    if plugin is None:
        return f"biff {ver}"
    return f"biff {ver} (plugin {plugin})"


def _print_version_and_exit(value: bool) -> None:
    if value:
        print(_version_line(*_versions()))
        raise typer.Exit


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    json_flag: Annotated[
        bool, typer.Option("--json", help="Output JSON instead of human-readable text.")
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Debug logging to stderr.")
    ] = False,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Suppress product command output.")
    ] = False,
    user: Annotated[
        str | None, typer.Option("--user", help="Run as this user.")
    ] = None,
    session: Annotated[
        str | None,
        typer.Option(
            "--session",
            "-s",
            envvar="BIFF_SESSION",
            help="Run as this session (name or id). Never creates one.",
        ),
    ] = None,
    no_telemetry: Annotated[
        bool,
        typer.Option("--no-telemetry", help="Disable usage capture for this process."),
    ] = False,
    version_flag: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Print the package and plugin versions and exit.",
            callback=_print_version_and_exit,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Biff: team communication for software engineers."""
    # The eager option callback already printed and exited when set.
    del version_flag
    if verbose and quiet:
        raise typer.BadParameter("--verbose and --quiet are mutually exclusive.")

    global _json_output, _quiet_output, _user_override, _session_ref
    _json_output = json_flag
    _quiet_output = quiet
    _user_override = user
    _session_ref = session or None

    CliInvocation(verbose, no_telemetry, user).configure(ctx)
    _suppress_nats_noise()

    if ctx.invoked_subcommand is None:
        CliInvocation.validate_repl_output(json_output=json_flag, quiet_output=quiet)
        asyncio.run(_repl())


# ---------------------------------------------------------------------------
# REPL and talk — clients of biffd
# ---------------------------------------------------------------------------


async def _repl() -> None:
    """Run the interactive prompt as a client of biffd."""
    try:
        command = DaemonCommand.for_this_process(_user_override, _session_ref)
        await ReplSession.interactive(command)
    except KeyboardInterrupt:
        print()
    except DaemonCommand.FAILURES as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise typer.Exit(code=1) from None


# Product commands: each asks biffd to run the command by name; ``_run()``
# prints the text or JSON and sets the exit code.


def _run(
    call: Callable[[DaemonCommand], Awaitable[Answer]],
    *,
    user: str | None = None,
    repo: Path | None = None,
) -> None:
    """Run one command as a client of biffd; print its text or JSON.

    It runs as *user* and in *repo* when given, else as the invocation's own.
    """

    def make(invocation_user: str | None, session: str | None) -> DaemonCommand:
        return DaemonCommand.for_this_process(user or invocation_user, session, repo)

    _drive(call, make)


def _drive[C](
    call: Callable[[C], Awaitable[Delivery]],
    make: Callable[[str | None, str | None], C],
) -> None:
    """Build the command with *make*, run *call* on it, and print the result."""

    async def _inner() -> None:
        try:
            answer = await call(make(_user_override, _session_ref))
            await answer.deliver(
                ResultPrinter(json_output=_json_output, quiet=_quiet_output)
            )
        except DaemonCommand.FAILURES as exc:
            if _json_output:
                _print_json({"error": str(exc)})
            else:
                print(f"Error: {exc}", file=sys.stderr)
            raise typer.Exit(code=1) from None
        if answer.result.error:
            raise typer.Exit(code=1)

    asyncio.run(_inner())


@app.command()
def who() -> None:
    """List active team members and what they're working on."""
    _run(lambda d: d.call("who"))


@app.command()
def finger(
    user: Annotated[str, typer.Argument(help="User to query, e.g. kai or kai:tty1")],
) -> None:
    """Check what a user is working on and their availability."""
    _run(lambda d: d.call("finger", user=user))


@app.command("write")
def write_cmd(
    to: Annotated[str, typer.Argument(help="Recipient, e.g. kai or kai:tty1")],
    message: Annotated[str, typer.Argument(help="Message to send (auto-splits)")],
) -> None:
    """Send a message to a teammate's inbox."""
    _run(lambda d: d.call("write", to=to, message=message))


@app.command("read")
def read_cmd() -> None:
    """Check inbox for new messages. Marks all as read."""
    _run(lambda d: d.call("read"))


@app.command()
def plan(
    message: Annotated[str, typer.Argument(help="What you're working on")] = "",
    clear: Annotated[bool, typer.Option("--clear", help="Clear plan")] = False,
) -> None:
    """Set what you're currently working on."""
    if clear:
        _run(lambda d: d.plan(""))
    elif not message:
        print("Usage: biff plan <message> | biff plan --clear", file=sys.stderr)
        raise typer.Exit(code=1)
    else:
        _run(lambda d: d.plan(message))


@app.command("last")
def last_cmd(
    user: Annotated[str, typer.Argument(help="Filter by user (optional)")] = "",
    count: Annotated[int, typer.Option(help="Number of entries")] = 25,
) -> None:
    """Show session login/logout history."""
    _run(lambda d: d.call("last", user=user, count=count))


@app.command("wall")
def wall_cmd(
    message: Annotated[str, typer.Argument(help="Broadcast message")] = "",
    duration: Annotated[str, typer.Option(help="Duration (e.g. 30m, 2h, 1d)")] = "",
    clear: Annotated[bool, typer.Option("--clear", help="Remove active wall")] = False,
) -> None:
    """Post, read, or clear a team broadcast."""
    if ClientEnvironment.carries_relay_settings(os.environ):
        # A CI runner: no session, no inbox, no daemon, relay handed in explicitly.
        _drive(
            lambda d: d.wall(message, duration, clear=clear),
            DirectWall.for_this_process,
        )
    else:
        _run(lambda d: d.call("wall", message=message, duration=duration, clear=clear))


@app.command()
def mesg(
    enabled: Annotated[
        str,
        typer.Argument(help="on/off (or y/n) to accept or block messages"),
    ],
) -> None:
    """Control message reception (on/off/y/n)."""
    _run(lambda d: d.call("mesg", value=enabled))


@app.command("tty")
def tty_cmd(
    name: Annotated[str, typer.Argument(help="Session name (optional)")] = "",
) -> None:
    """Name the current CLI session."""
    _run(lambda d: d.tty(name))


@app.command()
def status() -> None:
    """Show connection state, session info, and pending messages."""
    _run(lambda d: d.call("status"))


@app.command()
def watch() -> None:
    """Print one JSON line per arrival summary (mail, wall) until interrupted."""
    raise typer.Exit(
        code=asyncio.run(WatchCommand.watch_this_process(_user_override, _session_ref))
    )


app.add_typer(KeyCli.on_this_machine().typer_app(), name="key")


# ---------------------------------------------------------------------------
# Session verbs
# ---------------------------------------------------------------------------


@session_app.command("new")
def session_new(
    tty: Annotated[
        str | None,
        typer.Option("--tty", help="Exact session name; errors if taken."),
    ] = None,
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Repo for the session (default: current)."),
    ] = None,
    as_user: Annotated[
        str | None,
        typer.Option("--as", help="Identity for this session only."),
    ] = None,
) -> None:
    """Create a session and make it this repo's default."""
    _run(lambda d: d.session_new(tty or ""), user=as_user, repo=repo)


@session_app.command("ls")
def session_ls(
    all_flag: Annotated[
        bool,
        typer.Option("--all", help="Include ended sessions still retained."),
    ] = False,
) -> None:
    """List this host's sessions."""
    _run(lambda d: d.session_ls(include_ended=all_flag))


@session_app.command("use")
def session_use(
    name: Annotated[str, typer.Argument(help="Session name or id")],
) -> None:
    """Make a session this repo's default without attaching to it."""
    _run(lambda d: d.session_use(name))


@session_app.command("end")
def session_end(
    name: Annotated[str, typer.Argument(help="Session name or id")],
) -> None:
    """End a session; its unread mail moves to your user mailbox."""
    _run(lambda d: d.session_end(name))


# ---------------------------------------------------------------------------
# Admin commands
# ---------------------------------------------------------------------------


@app.command("version")
def version() -> None:
    """Print the biff version."""
    ver, plugin = _versions()
    if _json_output:
        _print_json({"version": ver, "plugin_version": plugin})
        return
    print(_version_line(ver, plugin))


@app.command("install-statusline")
def install_statusline() -> None:
    """Install biff into Claude Code's status bar."""
    from biff.plugins.claude_code.statusline import install as do_install

    result = do_install()
    print(result.message)
    if not result.installed:
        raise typer.Exit(code=1)


@app.command("uninstall-statusline")
def uninstall_statusline() -> None:
    """Remove biff from Claude Code's status bar."""
    from biff.plugins.claude_code.statusline import uninstall as do_uninstall

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
    (the beads ``bd setup`` model), and allows biff's MCP tools and commands
    in this repo's ``.claude/settings.json``, printing each rule added.
    Equivalent to the MCP ``/biff enable`` toggle — both do exactly this.
    Idempotent.  The committed files are tracked: commit them via a PR so
    every contributor participates; the git hooks are per-clone and never
    committed.  Invokes git only read-only (to
    resolve the hooks directory) and never creates commits.
    """
    repo_root = find_git_root(start)
    if repo_root is None:
        raise SystemExit("Not in a git repository. Run this from inside a repo.")

    from biff.enablement import RepoEnablement
    from biff.git_hooks import HOOKS_DIR_UNRESOLVED_NOTICE
    from biff.relay_check import LocalRelayCheck

    change = RepoEnablement(repo_root, CliInvocation.plugin_registry()).enable()
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
    for line in change.enable_report():
        print(line)
    print(LocalRelayCheck.enable_note(repo_root), end="")
    if change.failed:
        raise typer.Exit(code=1)


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
    ``.git/hooks`` biff dispatchers and ``.claude/settings.json`` allow
    rules.  Equivalent to the MCP ``/biff disable``
    toggle.  Idempotent.  Commit the removal of the tracked files via a PR
    for it to take effect for every contributor.  Invokes git only read-only
    (to resolve the hooks directory) and never creates commits.
    """
    repo_root = find_git_root(start)
    if repo_root is None:
        raise SystemExit("Not in a git repository. Run this from inside a repo.")

    from biff.enablement import RepoEnablement

    change = RepoEnablement(repo_root, CliInvocation.plugin_registry()).disable()
    if change.ci_workflow_changed:
        print("CI workflow removed: biff-notify.yml")
    if change.git_hooks_changed:
        print(f"Git hooks removed: {', '.join(change.git_hooks_changed)}")
    for line in change.disable_report():
        print(line)
    if change.failed:
        raise typer.Exit(code=1)


_PLUGIN_ID = "biff@punt-labs"


def _remove_repo_git_hooks() -> None:
    """Remove biff's git hooks from the current clone's ``.git/hooks/``.

    Skips silently outside a git repo.
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
    """Install biff via the punt-labs marketplace (machine scope; no repo writes)."""
    import shutil

    from biff.cli_install import MachineScope
    from biff.supervision.runner import Supervisor

    # A failed service install must not strand the plugin: nothing uses biffd yet.
    service_failed = MachineScope.provision()

    claude = shutil.which("claude")
    if not claude:
        # CLI-only install is a SUCCESS, not a partial failure: the biff CLI
        # and MCP server are installed; only the Claude Code plugin is skipped
        # (install-cli-only.md, matching install.sh's --no-plugin path). No
        # "restart Claude Code" line — there's no plugin.
        print("Installed (CLI-only). Claude Code not found; plugin step skipped.")
        raise typer.Exit(code=int(service_failed))

    code = Supervisor.run_visible(
        claude, "plugin", "install", _PLUGIN_ID, "--scope", "user"
    )
    # Bitwise or: a claude killed by a signal has a negative return code.
    if code | service_failed:
        raise typer.Exit(code=1)
    print("Installed. Restart Claude Code to activate.")


@app.command()
def doctor() -> None:
    """Check biff installation health."""
    from biff.cli_daemon_doctor import DaemonDiagnostics

    code = DaemonDiagnostics(CliInvocation.plugin_registry(), _user_override).run()
    if code != 0:
        raise typer.Exit(code=code)


@app.command("uninstall")
def uninstall_cmd() -> None:
    """Uninstall biff: remove the plugin, this clone's git hooks, and the import."""
    import shutil

    from biff.cli_install import MachineScope
    from biff.supervision.runner import Supervisor
    from biff.user_scope import USER_IMPORT_LINE, UserScope

    plugin_failed = False
    claude = shutil.which("claude")
    if claude:
        uninstall_args = (claude, "plugin", "uninstall", _PLUGIN_ID, "--scope", "user")
        plugin_failed = Supervisor.run_visible(*uninstall_args) != 0
    else:
        print("claude CLI not found; skipping plugin uninstall.")

    # Per-clone git hooks (deployed by `biff install` or `biff enable`) are
    # local machinery, so uninstall removes them for this clone. The committed
    # marker/CI workflow are repo policy — left untouched; `biff disable` owns
    # those (it also removes this clone's hooks).
    _remove_repo_git_hooks()
    plugin_failed |= MachineScope.deprovision()

    # Legacy strip (§2.6): older releases wrote a user-scope @-import. It is
    # removed here opportunistically and ALWAYS runs — never gated on the plugin
    # step — because a dangling @-import 404s every session. Current releases
    # write no such line, so this is a no-op on a clean machine.
    if UserScope().uninstall():
        print(f"Removed {USER_IMPORT_LINE} from ~/.claude/CLAUDE.md")

    if plugin_failed:
        # Surface the plugin failure — but only after the cleanup above ran.
        print("Warning: a removal step failed (see above); user-scope import removed.")
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
    from biff.plugins.claude_code.statusline import run_statusline

    print(run_statusline())


@app.command()
def talk(
    to: Annotated[
        str,
        typer.Argument(help="User to talk to, e.g. jmf-pobox:tty2"),
    ],
    message: Annotated[
        str,
        typer.Argument(help="Opening message (optional)."),
    ] = "",
) -> None:
    """Start an interactive talk with a teammate or agent.

    Invites the session, or accepts its invite, then opens a real-time
    conversation: type a line and press Enter to send, ``end`` or Ctrl+C to hang
    up.  biffd holds the conversation, so it survives this process.
    """
    talked = True  # a Ctrl+C before the talk is refused is a normal end
    try:
        command = DaemonCommand.for_this_process(_user_override, _session_ref)
        talked = asyncio.run(TalkConversation.alone(command, to, message))
    except KeyboardInterrupt:
        print("\nTalk session ended.")
    except DaemonCommand.FAILURES as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise typer.Exit(code=1) from None
    if not talked:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
