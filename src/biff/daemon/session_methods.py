"""The ``session`` verbs as daemon methods, over the daemon's session records."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from biff.commands import CommandResult
from biff.daemon.arrivals_reading import ArrivalsReading
from biff.daemon.session_end import SessionEnd
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

    __slots__ = ("_ender", "_live", "_store", "_table")

    _store: CliSessionStore
    _ender: SessionEnd
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
        self._ender = SessionEnd(live)
        self._table = {
            "session_new": self._new,
            "session_ls": self._ls,
            "session_use": self._use,
            "session_end": self._ender.end_shell,
            "end": self._ender.end,
            "arrivals": ArrivalsReading.handle,
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
