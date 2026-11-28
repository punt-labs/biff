"""The daemon's methods: one per command object, each returning wire data."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Self, final

from pydantic import JsonValue, TypeAdapter
from pydantic_core import to_jsonable_python

from biff import commands
from biff.daemon.activity_count import ActivityCount
from biff.daemon.params import Params
from biff.daemon.plan_method import PlanMethod
from biff.daemon.read_batch import ReadBatcher
from biff.daemon.read_steps import ReadSteps
from biff.daemon.refusal import RefusalError
from biff.daemon.talk_methods import TalkMethods
from biff.daemon.uncancellable import Uncancellable
from biff.frame_base import MAX_FRAME_BYTES
from biff.frame_reply import ErrorCode

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from biff.cli_context import CliContext
    from biff.commands import CommandResult
    from biff.daemon.session_methods import SessionMethods
    from biff.daemon.sessions import Session

__all__ = ["READ_BUDGET_BYTES", "Acknowledge", "Methods", "Outcome"]

_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)

type Handler = Callable[[CliContext, Params], Awaitable[CommandResult]]
type Acknowledge = Callable[[], Awaitable[None]]

_POLLS = frozenset({"status", "talk_read"})
"""The methods that are not work: an empty look must not move the activity clock
that routes mail (a ``read`` is one too, and stamps only when it finds mail)."""

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

    __slots__ = ("_annotate_status", "_read_budget", "_sessions", "_table")

    _table: dict[str, Handler]
    _sessions: SessionMethods
    _read_budget: int
    _annotate_status: Callable[[CommandResult], CommandResult]

    def __new__(
        cls,
        sessions: SessionMethods,
        read_budget: int = READ_BUDGET_BYTES,
        *,
        annotate_status: Callable[[CommandResult], CommandResult],
    ) -> Self:
        """Create the table of every served method.

        *sessions* serves the ``session_*`` methods.  *read_budget* is the most
        mail, in bytes of JSON, one ``read`` reply carries; the rest stays
        unread for the next ``read``.
        """
        self = super().__new__(cls)
        self._sessions = sessions
        self._read_budget = read_budget
        self._annotate_status = annotate_status
        self._table = {
            "who": Methods._who,
            "finger": Methods._finger,
            "write": Methods._write,
            "plan": PlanMethod.plan,
            "last": Methods._last,
            "wall": Methods._wall,
            "mesg": Methods._mesg,
            "tty": Methods._tty,
            "status": self._status,
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
        async with session.requests(method):
            if method == "read":
                return await self._read(session, Params(method, raw))
            if method not in _POLLS:
                await ActivityCount.of(session)
            if method in self._sessions.names:
                result = await self._sessions.call(session, method, Params(method, raw))
                return self._outcome(result)
            if method in TalkMethods.GATED and not await session.talk.ready(
                TalkMethods.READY_SECONDS
            ):
                return self._outcome(TalkMethods.NOT_READY)
            handler = self._table.get(method)
            if handler is None:
                msg = f"unknown method {method!r}; served: {', '.join(self.names)}"
                raise RefusalError.of(ErrorCode.UNKNOWN_METHOD, msg)
            return self._outcome(await handler(session.context, Params(method, raw)))

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

        The request timeout cancels the caller, not the read: a fetch that
        consumed mail runs on to where the mail is held.
        """
        p.done()
        return await Uncancellable.holding(session, self._read_locked, "read")

    async def _read_locked(self, session: Session) -> Outcome:
        if session.is_ended:
            msg = "session ended; start a new session"
            raise RefusalError.of(ErrorCode.SESSION_ENDED, msg)
        inbox = await ReadSteps.unread(session)
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
        left -= await ReadSteps.refile(session, inbox, pending, batch)
        left += await ReadSteps.backlog(inbox)
        if left > 0:
            result["more"] = left
        if batch:
            await ActivityCount.of(session)
        ack = partial(ReadSteps.delivered, session, inbox, batch) if batch else None
        return Outcome(result, ack)

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
    async def _last(ctx: CliContext, p: Params) -> CommandResult:
        user, count = p.text("user", ""), p.integer("count", 25)
        p.done()
        return await commands.last(ctx, user, count)

    @staticmethod
    async def _wall(ctx: CliContext, p: Params) -> CommandResult:
        message, duration = p.text("message", ""), p.text("duration", "")
        clear, repo = p.flag("clear", default=False), p.text("repo", "")
        p.done()
        return await commands.wall(ctx, message, duration, clear=clear, repo=repo)

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

    async def _status(self, ctx: CliContext, p: Params) -> CommandResult:
        p.done()
        return self._annotate_status(await commands.status(ctx))
