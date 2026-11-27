"""Command line parser for the REPL, and the in-process runner for a parsed line.

:func:`parse` turns a command line into the :class:`Call` biffd runs by name::

    call = parse("write kai 'ready for review'")
    # Call(method="write", params={"to": "kai", "message": "ready for review"})

:func:`dispatch` parses a line and runs the call in this process::

    result = await dispatch("write kai 'ready for review'", ctx)
    print(result.text)

Uses :mod:`shlex` for shell-style argument splitting so quoted
strings work naturally.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from pydantic import JsonValue

from biff import commands
from biff.cli_context import CliContext
from biff.cli_plan import CallerPlan
from biff.commands import CommandResult

_DURATION_RE = re.compile(r"^\d+[mhd]$")

type Params = dict[str, JsonValue]
type Parser = Callable[[list[str]], Call | CommandResult]
type Runner = Callable[[CliContext, Mapping[str, JsonValue]], Awaitable[CommandResult]]


@dataclass(frozen=True)
class Call:
    """A command biffd runs by name: the method and its wire params."""

    method: str
    params: Params


def _usage(text: str) -> CommandResult:
    return CommandResult(text=text, error=True)


def _who(args: list[str]) -> Call | CommandResult:
    return _usage("Usage: who") if args else Call("who", {})


def _finger(args: list[str]) -> Call | CommandResult:
    if len(args) != 1:
        return _usage("Usage: finger user")
    return Call("finger", {"user": args[0]})


def _write(args: list[str]) -> Call | CommandResult:
    if len(args) < 2:
        return _usage("Usage: write user message")
    return Call("write", {"to": args[0], "message": " ".join(args[1:])})


def _read(args: list[str]) -> Call | CommandResult:
    return _usage("Usage: read") if args else Call("read", {})


def _plan(args: list[str]) -> Call | CommandResult:
    if not args:
        return _usage("Usage: plan message | plan clear")
    return Call("plan", {"message": "" if args == ["clear"] else " ".join(args)})


def _last(args: list[str]) -> Call | CommandResult:
    user = ""
    count = 25
    seen_user = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--count":
            if i + 1 >= len(args):
                return _usage("Usage: last [--count N] [user]")
            try:
                count = int(args[i + 1])
            except ValueError:
                return _usage("--count must be a number")
            i += 2
        elif arg.startswith("-"):
            return _usage("Usage: last [--count N] [user]")
        else:
            if seen_user:
                return _usage("Usage: last [--count N] [user]")
            user = arg
            seen_user = True
            i += 1
    return Call("last", {"user": user, "count": count})


def _wall(args: list[str]) -> Call | CommandResult:
    if not args:
        return Call("wall", {"message": "", "duration": "", "clear": False})
    if args[0] == "clear":
        if len(args) > 1:
            return _usage("Usage: wall clear")
        return Call("wall", {"message": "", "duration": "", "clear": True})
    # If the last token looks like a duration (e.g., 30m, 2h, 1d),
    # treat it as the duration and join the rest as the message.
    duration = ""
    if len(args) > 1 and _DURATION_RE.match(args[-1]):
        duration = args[-1]
        args = args[:-1]
    return Call(
        "wall", {"message": " ".join(args), "duration": duration, "clear": False}
    )


def _mesg(args: list[str]) -> Call | CommandResult:
    if len(args) != 1:
        return _usage("Usage: mesg on|off|y|n")
    return Call("mesg", {"value": args[0]})


def _tty(args: list[str]) -> Call | CommandResult:
    if len(args) > 1:
        return _usage("Usage: tty [name]")
    return Call("tty", {"name": args[0] if args else ""})


def _status(args: list[str]) -> Call | CommandResult:
    return _usage("Usage: status") if args else Call("status", {})


_PARSERS: dict[str, Parser] = {
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


def _text(params: Mapping[str, JsonValue], name: str) -> str:
    value = params[name]
    return value if isinstance(value, str) else str(value)


async def _run_wall(ctx: CliContext, params: Mapping[str, JsonValue]) -> CommandResult:
    return await commands.wall(
        ctx,
        _text(params, "message"),
        _text(params, "duration"),
        clear=params["clear"] is True,
    )


async def _run_last(ctx: CliContext, params: Mapping[str, JsonValue]) -> CommandResult:
    count = params["count"]
    return await commands.last(
        ctx, _text(params, "user"), count if isinstance(count, int) else 25
    )


_RUNNERS: dict[str, Runner] = {
    "who": lambda ctx, _: commands.who(ctx),
    "finger": lambda ctx, p: commands.finger(ctx, _text(p, "user")),
    "write": lambda ctx, p: commands.write(ctx, _text(p, "to"), _text(p, "message")),
    "read": lambda ctx, _: commands.read(ctx),
    "plan": lambda ctx, p: CallerPlan.set(ctx, _text(p, "message")),
    "last": _run_last,
    "wall": _run_wall,
    "mesg": lambda ctx, p: commands.mesg(ctx, _text(p, "value")),
    "tty": lambda ctx, p: commands.tty(ctx, _text(p, "name")),
    "status": lambda ctx, _: commands.status(ctx),
}


def available_commands() -> list[str]:
    """Return sorted list of available command names."""
    return sorted(_PARSERS)


def parse(line: str) -> Call | CommandResult | None:
    """Parse a command line into the call it names.

    Returns ``None`` for the ``exit``/``quit`` command (case-insensitive).
    Returns ``CommandResult(text="")`` for empty/whitespace input, and an error
    ``CommandResult`` for a line that is not a command or is misused.
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
    parser = _PARSERS.get(cmd_name)
    if parser is None:
        cmds = ", ".join(available_commands())
        return CommandResult(
            text=f"Unknown command: {cmd_name}. Available: {cmds}",
            error=True,
        )

    return parser(tokens[1:])


async def dispatch(line: str, ctx: CliContext) -> CommandResult | None:
    """Parse and execute a command line in this process.

    Returns ``None`` for the ``exit``/``quit`` command (case-insensitive).
    Returns ``CommandResult(text="")`` for empty/whitespace input.
    Returns a ``CommandResult`` for all other input.
    """
    parsed = parse(line)
    if not isinstance(parsed, Call):
        return parsed
    return await _RUNNERS[parsed.method](ctx, parsed.params)
