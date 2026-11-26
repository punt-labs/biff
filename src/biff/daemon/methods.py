"""The daemon's methods: one per command object, each returning wire data."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Self, final

from biff import commands
from biff.daemon.refusal import RefusalError
from biff.frame_reply import ErrorCode

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pydantic import JsonValue

    from biff.cli_context import CliContext
    from biff.commands import CommandResult

__all__ = ["Methods", "Params"]

logger = logging.getLogger(__name__)

type Handler = Callable[[CliContext, Params], Awaitable[CommandResult]]


@final
class Params:
    """A request's params, read by name and checked to be fully consumed.

    A method reads every param it accepts and then calls :meth:`done`, so a
    param it does not know, such as another session's key, is refused instead
    of silently ignored.
    """

    __slots__ = ("_method", "_raw", "_seen")

    _method: str
    _raw: dict[str, JsonValue]
    _seen: set[str]

    def __new__(cls, method: str, raw: dict[str, JsonValue]) -> Self:
        """Wrap *raw*, the params of a request for *method*."""
        self = super().__new__(cls)
        self._method = method
        self._raw = raw
        self._seen = set()
        return self

    def text(self, name: str, default: str | None = None) -> str:
        """Return the string param *name*, or *default* when absent.

        Raises:
            RefusalError: The param is absent with no default, or not a string.
        """
        self._seen.add(name)
        if name not in self._raw:
            if default is None:
                raise self._invalid(f"{name} is required")
            return default
        value = self._raw[name]
        if not isinstance(value, str):
            raise self._invalid(f"{name} must be a string")
        return value

    def integer(self, name: str, default: int) -> int:
        """Return the integer param *name*, or *default* when absent.

        Raises:
            RefusalError: The param is not an integer.
        """
        self._seen.add(name)
        value = self._raw.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise self._invalid(f"{name} must be an integer")
        return value

    def flag(self, name: str, *, default: bool) -> bool:
        """Return the boolean param *name*, or *default* when absent.

        Raises:
            RefusalError: The param is not a boolean.
        """
        self._seen.add(name)
        value = self._raw.get(name, default)
        if not isinstance(value, bool):
            raise self._invalid(f"{name} must be a boolean")
        return value

    def done(self) -> None:
        """Refuse any param the method did not read.

        Raises:
            RefusalError: A param is present that the method does not accept.
        """
        unknown = sorted(set(self._raw) - self._seen)
        if unknown:
            raise self._invalid(f"unknown param {', '.join(unknown)}")

    def _invalid(self, why: str) -> RefusalError:
        return RefusalError.of(ErrorCode.INVALID_PARAMS, f"{self._method}: {why}")


@final
class Methods:
    """Dispatches a method name to its command object."""

    __slots__ = ("_table",)

    _table: dict[str, Handler]

    def __new__(cls) -> Self:
        """Create the table of every served method."""
        self = super().__new__(cls)
        self._table = {
            "who": Methods._who,
            "finger": Methods._finger,
            "write": Methods._write,
            "read": Methods._read,
            "plan": Methods._plan,
            "last": Methods._last,
            "wall": Methods._wall,
            "mesg": Methods._mesg,
            "tty": Methods._tty,
            "status": Methods._status,
        }
        return self

    @property
    def names(self) -> tuple[str, ...]:
        """The served method names."""
        return tuple(self._table)

    async def call(
        self, ctx: CliContext, method: str, raw: dict[str, JsonValue]
    ) -> JsonValue:
        """Run *method* for the session *ctx* and return its wire result.

        The result is ``{"data": <the command's JSON payload>, "error": bool}``;
        display text is the client's to render from the data.

        Raises:
            RefusalError: The method is unknown or its params are invalid.
        """
        handler = self._table.get(method)
        if handler is None:
            msg = f"unknown method {method!r}; served: {', '.join(self.names)}"
            raise RefusalError.of(ErrorCode.UNKNOWN_METHOD, msg)
        result = await handler(ctx, Params(method, raw))
        data: JsonValue = json.loads(json.dumps(result.json_data, default=str))
        return {"data": data, "error": result.error}

    @staticmethod
    async def _who(ctx: CliContext, p: Params) -> CommandResult:
        p.done()
        return await commands.who(ctx)

    @staticmethod
    async def _finger(ctx: CliContext, p: Params) -> CommandResult:
        user = p.text("user")
        p.done()
        return await commands.finger(ctx, user)

    @staticmethod
    async def _write(ctx: CliContext, p: Params) -> CommandResult:
        to, message = p.text("to"), p.text("message")
        p.done()
        return await commands.write(ctx, to, message)

    @staticmethod
    async def _read(ctx: CliContext, p: Params) -> CommandResult:
        p.done()
        return await commands.read(ctx)

    @staticmethod
    async def _plan(ctx: CliContext, p: Params) -> CommandResult:
        message = p.text("message")
        p.done()
        return await commands.plan(ctx, message)

    @staticmethod
    async def _last(ctx: CliContext, p: Params) -> CommandResult:
        user, count = p.text("user", ""), p.integer("count", 25)
        p.done()
        return await commands.last(ctx, user, count)

    @staticmethod
    async def _wall(ctx: CliContext, p: Params) -> CommandResult:
        message, duration = p.text("message", ""), p.text("duration", "")
        clear = p.flag("clear", default=False)
        p.done()
        return await commands.wall(ctx, message, duration, clear=clear)

    @staticmethod
    async def _mesg(ctx: CliContext, p: Params) -> CommandResult:
        value = p.text("value")
        p.done()
        return await commands.mesg(ctx, value)

    @staticmethod
    async def _tty(ctx: CliContext, p: Params) -> CommandResult:
        name = p.text("name", "")
        p.done()
        return await commands.tty(ctx, name)

    @staticmethod
    async def _status(ctx: CliContext, p: Params) -> CommandResult:
        p.done()
        return await commands.status(ctx)
