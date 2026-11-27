"""The daemon's methods: one per command object, each returning wire data."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Self, final

from pydantic import JsonValue, TypeAdapter
from pydantic_core import to_jsonable_python

from biff import commands
from biff.commands.inbox import Inbox
from biff.daemon.refusal import RefusalError
from biff.formatting import format_read
from biff.frame_base import MAX_FRAME_BYTES
from biff.frame_reply import ErrorCode
from biff.relay_errors import RELAY_ERRORS

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from biff.cli_context import CliContext
    from biff.commands import CommandResult
    from biff.daemon.sessions import Session
    from biff.models import Message

__all__ = ["READ_BUDGET_BYTES", "Acknowledge", "Methods", "Outcome", "Params"]

logger = logging.getLogger(__name__)

_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)

type Handler = Callable[[CliContext, Params], Awaitable[CommandResult]]
type Acknowledge = Callable[[], Awaitable[None]]

READ_BUDGET_BYTES = MAX_FRAME_BYTES // 2
"""The most one ``read`` reply carries, rows and rendered text together, in bytes.

Half the frame limit leaves the other half for the frame's own fields, so a
batch can never be refused by the client.
"""

_ENVELOPE_BYTES = 512
"""What a reply adds around its messages: ids, keys, the ``more`` count."""

_CLIPPED = "[clipped]"


@dataclass(frozen=True)
class Outcome:
    """A method's wire result, and what to do once that result has been delivered.

    ``acknowledge`` is for effects that must not happen if the reply is lost:
    marking mail read.  The connection runs it only after the reply was written.
    """

    result: JsonValue
    acknowledge: Acknowledge | None = None


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

    __slots__ = ("_read_budget", "_table")

    _table: dict[str, Handler]
    _read_budget: int

    def __new__(cls, read_budget: int = READ_BUDGET_BYTES) -> Self:
        """Create the table of every served method.

        *read_budget* is the most mail, in bytes of JSON, one ``read`` reply
        carries; the rest stays unread for the next ``read``.
        """
        self = super().__new__(cls)
        self._read_budget = read_budget
        self._table = {
            "who": Methods._who,
            "finger": Methods._finger,
            "write": Methods._write,
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
        return ("read", *self._table)

    async def call(
        self, session: Session, method: str, raw: dict[str, JsonValue]
    ) -> Outcome:
        """Run *method* for the session *ctx* and return its wire result.

        The result is ``{"data": <the command's JSON payload>, "error": bool,
        "text": <the command's display text>}``.  The engine renders the text once,
        so every client prints the same words.  A payload that cannot be
        serialised raises, rather than being turned into a string.

        Raises:
            RefusalError: The method is unknown or its params are invalid.
        """
        ctx = session.context
        if method == "read":
            return await self._read(session, Params(method, raw))
        handler = self._table.get(method)
        if handler is None:
            msg = f"unknown method {method!r}; served: {', '.join(self.names)}"
            raise RefusalError.of(ErrorCode.UNKNOWN_METHOD, msg)
        result = await handler(ctx, Params(method, raw))
        data = _JSON.validate_python(to_jsonable_python(result.json_data))
        return Outcome({"data": data, "error": result.error, "text": result.text})

    async def _read(self, session: Session, p: Params) -> Outcome:
        """Return the next batch of unread mail, acknowledged once delivered.

        Fetched mail is held in the session's outbox, because some relays consume
        mail at fetch: what a lost reply or a truncated batch leaves behind is
        served by the next ``read``.  The batch is as much as fits the read
        budget in bytes; a message larger than the budget alone is clipped, never
        sent whole.  ``more`` counts what the batch leaves: held mail and mail
        still on the relay behind a bounded fetch.
        """
        p.done()
        inbox = await Inbox.unread(session.context)
        pending = session.outbox.merge(inbox.messages)
        batch, data, text = self._fit(pending)
        result: dict[str, JsonValue] = {"data": data, "error": False, "text": text}
        # The backlog summary is a hint for ``more``; a relay that fails to report
        # it must not sink a batch that was already fetched and is deliverable.
        left = len(pending) - len(batch)
        try:
            left += await inbox.unfetched()
        except RELAY_ERRORS as exc:
            logger.warning("read backlog summary unavailable: %s", exc)
        if left > 0:
            result["more"] = left
        if not batch:
            return Outcome(result)
        return Outcome(result, partial(self._delivered, session, inbox, batch))

    def _fit(self, pending: list[Message]) -> tuple[list[Message], JsonValue, str]:
        """Return the batch, its rows and its text, as one reply that fits the budget.

        The text is the table the rows render as, and it travels in the same
        frame, so the budget covers rows and text together.  Bound: the loop
        ends only with ``rows + text + envelope <= read_budget``, which is at
        most half the frame limit, so the reply's own fields (id, type, version,
        ``more``) always fit in the other half.  Each pass shrinks the room by
        at least a byte, so it ends; at the room of one clipped message the reply
        is as small as a reply can be, and a budget below that has no solution.
        """
        room = self._read_budget
        while True:
            batch = self._batch(pending, room)
            rows = [m.model_dump(mode="json") for m in batch]
            data = _JSON.validate_python(to_jsonable_python(rows))
            text = format_read(batch) if batch else "No new messages."
            reply = {"data": data, "error": False, "text": text}
            size = len(json.dumps(reply, ensure_ascii=False).encode())
            if size <= self._read_budget or room <= _ENVELOPE_BYTES:
                return batch, data, text
            # Text and rows grow together, so scale the room, not subtract.
            room = min(room - 1, room * self._read_budget // size)

    @staticmethod
    def _batch(pending: list[Message], room: int) -> list[Message]:
        """Take messages from the front of *pending* while their rows fit *room*."""
        batch: list[Message] = []
        used = _ENVELOPE_BYTES
        for message in pending:
            size = len(message.model_dump_json().encode()) + 1  # the comma
            if not batch and used + size > room:
                message = Methods._clip(message, room - used)
                size = len(message.model_dump_json().encode()) + 1
            elif used + size > room:
                break
            batch.append(message)
            used += size
        return batch

    @staticmethod
    def _clip(message: Message, room: int) -> Message:
        """Shorten *message*'s body until the message fits *room* bytes of JSON."""
        bare = len(message.model_copy(update={"body": "x"}).model_dump_json().encode())
        keep = max(1, room - bare - len(_CLIPPED.encode()))
        body = message.body.encode()[:keep].decode(errors="ignore") or "x"
        return message.model_copy(update={"body": body + _CLIPPED})

    @staticmethod
    async def _delivered(session: Session, inbox: Inbox, batch: list[Message]) -> None:
        """Forget the batch and mark it read, once the reply that carried it left."""
        session.outbox.delivered(batch)
        await inbox.mark_read(batch)

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
