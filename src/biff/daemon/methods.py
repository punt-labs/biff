"""The daemon's methods: one per command object, each returning wire data."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Self, final

from pydantic import JsonValue, TypeAdapter
from pydantic_core import to_jsonable_python

from biff import commands
from biff.commands.inbox import Inbox
from biff.daemon.params import Params
from biff.daemon.read_batch import ReadBatcher
from biff.daemon.refusal import RefusalError
from biff.daemon.talk_methods import TalkMethods
from biff.frame_base import MAX_FRAME_BYTES
from biff.frame_reply import ErrorCode
from biff.relay_errors import RELAY_ERRORS

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from biff.cli_context import CliContext
    from biff.commands import CommandResult
    from biff.daemon.session_methods import SessionMethods
    from biff.daemon.sessions import Session
    from biff.models import Message

__all__ = ["READ_BUDGET_BYTES", "Acknowledge", "Methods", "Outcome"]

logger = logging.getLogger(__name__)

_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)

type Handler = Callable[[CliContext, Params], Awaitable[CommandResult]]
type Acknowledge = Callable[[], Awaitable[None]]

READ_BUDGET_BYTES = MAX_FRAME_BYTES // 2
"""The most one ``read`` reply carries, rows and rendered text together, in bytes.

Half the frame limit leaves the other half for the frame's own fields, so a
batch can never be refused by the client.
"""


@dataclass(frozen=True)
class Outcome:
    """A method's wire result, and what to do once that result has been delivered.

    ``acknowledge`` is for effects that must not happen if the reply is lost:
    marking mail read.  The connection runs it only after the reply was written.
    """

    result: JsonValue
    acknowledge: Acknowledge | None = None


@final
class Methods:
    """Dispatches a method name to its command object."""

    __slots__ = ("_read_budget", "_sessions", "_table")

    _table: dict[str, Handler]
    _sessions: SessionMethods
    _read_budget: int

    def __new__(
        cls, sessions: SessionMethods, read_budget: int = READ_BUDGET_BYTES
    ) -> Self:
        """Create the table of every served method.

        *sessions* serves the ``session_*`` methods.  *read_budget* is the most
        mail, in bytes of JSON, one ``read`` reply carries; the rest stays
        unread for the next ``read``.
        """
        self = super().__new__(cls)
        self._sessions = sessions
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
            "talk": TalkMethods.talk,
            "talk_read": TalkMethods.talk_read,
            "talk_end": TalkMethods.talk_end,
        }
        return self

    @property
    def names(self) -> tuple[str, ...]:
        """The served method names."""
        return ("read", *self._table, *self._sessions.names)

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
        if method in self._sessions.names:
            result = await self._sessions.call(session, method, Params(method, raw))
            return self._outcome(result)
        handler = self._table.get(method)
        if handler is None:
            msg = f"unknown method {method!r}; served: {', '.join(self.names)}"
            raise RefusalError.of(ErrorCode.UNKNOWN_METHOD, msg)
        return self._outcome(await handler(ctx, Params(method, raw)))

    @staticmethod
    def _outcome(result: CommandResult) -> Outcome:
        data = _JSON.validate_python(to_jsonable_python(result.json_data))
        return Outcome({"data": data, "error": result.error, "text": result.text})

    async def _read(self, session: Session, p: Params) -> Outcome:
        """Return the next batch of unread mail, acknowledged once delivered.

        Fetched mail is held in the session's outbox until its reply is confirmed,
        because some relays consume mail at fetch: a lost reply is served again by
        the next ``read``.  What a truncated batch leaves behind goes back to such
        a relay, so everything that drains the session finds it.  The batch is as
        much as fits the read budget in bytes; a message larger than the budget
        alone is clipped, never sent whole.  ``more`` counts what the batch
        leaves: held mail and mail still on the relay behind a bounded fetch.
        """
        p.done()
        async with session.exclusive():
            return await self._read_locked(session)

    async def _read_locked(self, session: Session) -> Outcome:
        if session.is_ended:
            msg = "session ended; start a new session"
            raise RefusalError.of(ErrorCode.SESSION_ENDED, msg)
        inbox = await Inbox.unread(session.context)
        pending = session.outbox.merge(inbox.messages)
        fitted = ReadBatcher(self._read_budget).fit(pending)
        batch = fitted.messages
        result: dict[str, JsonValue] = {
            "data": fitted.data,
            "error": False,
            "text": fitted.text,
        }
        # The backlog summary is a hint for ``more``; a relay that fails to report
        # it must not sink a batch that was already fetched and is deliverable.
        # Mail given back to the relay is counted by the relay's own backlog.
        left = len(pending) - len(batch)
        left -= await self._refile(session, inbox, pending, batch)
        try:
            left += await inbox.unfetched()
        except RELAY_ERRORS as exc:
            logger.warning("read backlog summary unavailable: %s", exc)
        if left > 0:
            result["more"] = left
        if not batch:
            return Outcome(result)
        return Outcome(result, partial(self._delivered, session, inbox, batch))

    @staticmethod
    async def _refile(
        session: Session, inbox: Inbox, pending: list[Message], batch: list[Message]
    ) -> int:
        """Give the relay back the mail this batch leaves, if the relay consumed it.

        Return how many messages went back.

        Held in the daemon alone, that mail would be out of reach of everything
        else that drains or reads the session (ending it, another process), and
        gone if the daemon died.  The batch itself stays held until confirmed.
        """
        sent = {m.id for m in batch}
        left_behind = [m for m in pending if m.id not in sent]
        filed = 0
        for message in left_behind:
            try:
                if await inbox.refile(message):
                    # Released as it is filed: a read cancelled before the next
                    # refile must not leave this message in the relay and here.
                    session.outbox.forget([message])
                    filed += 1
            except RELAY_ERRORS:
                logger.warning("could not file message %s again", message.id)
        return filed

    @staticmethod
    async def _delivered(session: Session, inbox: Inbox, batch: list[Message]) -> None:
        """Forget the batch and mark it read, once the reply that carried it left."""
        session.outbox.forget(batch)
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
