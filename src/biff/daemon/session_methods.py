"""The ``session`` verbs as daemon methods, over the daemon's session records."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Self, cast, final

from biff.commands import CommandResult
from biff.daemon.session_host import SessionHost
from biff.daemon.session_pointer import CurrentPointer
from biff.daemon.session_select import SessionSelector
from biff.daemon.session_verbs import SessionVerbs
from biff.tty import get_hostname

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from biff.daemon.params import Params
    from biff.daemon.session_store import CliSessionStore
    from biff.daemon.sessions import Session, SessionTable

__all__ = ["SessionMethods"]

type Handler = Callable[[Session, SessionVerbs, Params], Awaitable[CommandResult]]


@final
class SessionMethods:
    """Runs ``session_new``, ``session_ls``, ``session_use`` and ``session_end``.

    The daemon owns the records: a caller sends a name or an id and gets back
    the record's row.  A verb runs against the relay and repository of the
    session that asked, and a refusal is a failed result, as for every other
    method.  The repo pointer stays with the caller, so no verb writes it.
    """

    __slots__ = ("_live", "_store", "_table")

    _store: CliSessionStore
    _live: SessionTable
    _table: dict[str, Handler]

    def __new__(cls, store: CliSessionStore, live: SessionTable) -> Self:
        """Serve the verbs over the records in *store*.

        *live* holds the sessions connections are attached to; ending a session
        retires its entry there.
        """
        self = super().__new__(cls)
        self._store = store
        self._live = live
        self._table = {
            "session_new": self._new,
            "session_ls": self._ls,
            "session_use": self._use,
            "session_end": self._end,
        }
        return self

    @property
    def names(self) -> tuple[str, ...]:
        """The served method names."""
        return tuple(self._table)

    async def call(self, session: Session, method: str, p: Params) -> CommandResult:
        """Run the verb *method* for *session*.

        Raises:
            KeyError: *method* is not one of :attr:`names`.
        """
        try:
            return await self._table[method](session, self._verbs(session), p)
        except ValueError as exc:
            msg = f"Error: {exc}"
            return CommandResult(text=msg, json_data={"error": msg}, error=True)

    def _verbs(self, session: Session) -> SessionVerbs:
        ctx = session.context
        hostname = get_hostname()
        selector = SessionSelector(
            store=self._store,
            pointer=CurrentPointer(self._store.root.parent / "current"),
            user=ctx.user,
            repo=ctx.config.repo_name,
            repo_root=ctx.repo_root or Path(session.cwd),
            hostname=hostname,
        )
        host = SessionHost(
            relay=ctx.relay,
            config=ctx.config,
            selector=selector,
            hostname=hostname,
            pwd=session.cwd,
        )
        return SessionVerbs(host)

    async def _new(
        self, _session: Session, verbs: SessionVerbs, p: Params
    ) -> CommandResult:
        tty = p.text("tty", "")
        p.done()
        return await verbs.new(tty or None)

    async def _ls(
        self, _session: Session, verbs: SessionVerbs, p: Params
    ) -> CommandResult:
        include_ended = p.flag("include_ended", default=False)
        p.done()
        return await verbs.ls(include_ended=include_ended)

    async def _use(
        self, _session: Session, verbs: SessionVerbs, p: Params
    ) -> CommandResult:
        ref = p.text("ref")
        p.done()
        return await verbs.use(ref)

    async def _end(
        self, session: Session, verbs: SessionVerbs, p: Params
    ) -> CommandResult:
        """File the mail the session holds back in the relay, end it, retire it.

        A consuming relay removed the mail the daemon fetched for the session, so
        a batch whose reply was never confirmed exists only in its outbox.  It goes
        back to the relay first, where ending drains it into the owner's mailbox
        with the rest.  A message the relay refuses stops the end, with the mail
        still held.  Connections attached to the ended session are told, so a
        watcher does not keep listening at an address that no longer exists.
        """
        ref = p.text("ref")
        p.done()
        selector = verbs.host.selector
        target = selector.lookup(ref).session_id
        held = self._live.holding(target)
        async with held.exclusive() if held is not None else nullcontext():
            # A name can change hands while this waits for a read: end the session
            # the caller named, and nothing else.
            if selector.lookup(ref).session_id != target:
                msg = f"session {ref} changed while waiting; nothing was ended"
                raise ValueError(msg)
            if held is not None:
                await held.refile_held()
            result = await verbs.end(target)
            if held is not None:
                held.mark_ended()
        row = cast("dict[str, str]", result.json_data)
        self._live.retire(row["id"], spare=session)
        return result
