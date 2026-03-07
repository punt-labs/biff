"""Command dispatcher for the REPL.

Parses a command line string into a command function call::

    result = await dispatch("write @kai 'ready for review'", ctx)
    print(result.text)

Uses :mod:`shlex` for shell-style argument splitting so quoted
strings work naturally.
"""

from __future__ import annotations

import shlex
from collections.abc import Awaitable, Callable

from biff import commands
from biff.cli_session import CliContext
from biff.commands import CommandResult

# Handler signature: (ctx, args) -> CommandResult
type Handler = Callable[[CliContext, list[str]], Awaitable[CommandResult]]


async def _who(ctx: CliContext, args: list[str]) -> CommandResult:
    if args:
        return CommandResult(text="Usage: who", error=True)
    return await commands.who(ctx)


async def _finger(ctx: CliContext, args: list[str]) -> CommandResult:
    if len(args) != 1:
        return CommandResult(text="Usage: finger @user", error=True)
    return await commands.finger(ctx, args[0])


async def _write(ctx: CliContext, args: list[str]) -> CommandResult:
    if len(args) < 2:
        return CommandResult(text='Usage: write @user "message"', error=True)
    to = args[0]
    message = " ".join(args[1:])
    return await commands.write(ctx, to, message)


async def _read(ctx: CliContext, args: list[str]) -> CommandResult:
    if args:
        return CommandResult(text="Usage: read", error=True)
    return await commands.read(ctx)


async def _plan(ctx: CliContext, args: list[str]) -> CommandResult:
    if not args:
        return CommandResult(text='Usage: plan "message"', error=True)
    message = " ".join(args)
    return await commands.plan(ctx, message)


async def _last(ctx: CliContext, args: list[str]) -> CommandResult:
    user = ""
    count = 25
    seen_user = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--count":
            if i + 1 >= len(args):
                return CommandResult(text="Usage: last [--count N] [@user]", error=True)
            try:
                count = int(args[i + 1])
            except ValueError:
                return CommandResult(text="--count must be a number", error=True)
            i += 2
        elif arg.startswith("-"):
            return CommandResult(text="Usage: last [--count N] [@user]", error=True)
        else:
            if seen_user:
                return CommandResult(text="Usage: last [--count N] [@user]", error=True)
            user = arg
            seen_user = True
            i += 1
    return await commands.last(ctx, user, count)


async def _wall(ctx: CliContext, args: list[str]) -> CommandResult:
    if args and args[0] == "--clear":
        return await commands.wall(ctx, "", "", clear=True)
    message = ""
    duration = ""
    if args:
        message = args[0]
    if len(args) > 1:
        duration = args[1]
    return await commands.wall(ctx, message, duration, clear=False)


async def _mesg(ctx: CliContext, args: list[str]) -> CommandResult:
    if len(args) != 1:
        return CommandResult(text="Usage: mesg on|off|y|n", error=True)
    return await commands.mesg(ctx, args[0])


async def _tty(ctx: CliContext, args: list[str]) -> CommandResult:
    name = args[0] if args else ""
    return await commands.tty(ctx, name)


async def _status(ctx: CliContext, args: list[str]) -> CommandResult:
    if args:
        return CommandResult(text="Usage: status", error=True)
    return await commands.status(ctx)


_COMMANDS: dict[str, Handler] = {
    "who": _who,
    "finger": _finger,
    "write": _write,
    "read": _read,
    "plan": _plan,
    "last": _last,
    "wall": _wall,
    "mesg": _mesg,
    "tty": _tty,
    "status": _status,
}


def available_commands() -> list[str]:
    """Return sorted list of available command names."""
    return sorted(_COMMANDS)


async def dispatch(line: str, ctx: CliContext) -> CommandResult | None:
    """Parse and execute a command line.

    Returns ``None`` for the ``exit``/``quit`` command (case-insensitive).
    Returns ``CommandResult(text="")`` for empty/whitespace input.
    Returns a ``CommandResult`` for all other input.
    """
    line = line.strip()
    if not line:
        return CommandResult(text="")

    if line.lower() in ("exit", "quit"):
        return None

    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return CommandResult(text=f"Parse error: {exc}", error=True)

    if not tokens:
        return CommandResult(text="")

    cmd_name = tokens[0].lower()
    args = tokens[1:]

    handler = _COMMANDS.get(cmd_name)
    if handler is None:
        cmds = ", ".join(available_commands())
        return CommandResult(
            text=f"Unknown command: {cmd_name}. Available: {cmds}",
            error=True,
        )

    return await handler(ctx, args)
