"""Ending a harness session: on its own request, or when retention has run out."""

from __future__ import annotations

import logging
from contextlib import nullcontext
from typing import TYPE_CHECKING, Self, cast, final

from biff.commands._result import CommandResult
from biff.commands.inbox import Inbox
from biff.daemon.refusal import RefusalError
from biff.daemon.session_drain import InboxDrain
from biff.daemon.session_key import SessionKey
from biff.frame_reply import ErrorCode
from biff.models import SessionEvent
from biff.relay import SESSION_TTL_SECONDS
from biff.relay_errors import RELAY_ERRORS
from biff.session_id import CLAUDE_CODE_ROLE, CLI_ROLE
from biff.tty import get_hostname

if TYPE_CHECKING:
    from biff.daemon.params import Params
    from biff.daemon.session_verbs import SessionVerbs
    from biff.daemon.sessions import Session, SessionTable

__all__ = ["RETENTION_SECONDS", "SessionEnd"]

logger = logging.getLogger(__name__)

RETENTION_SECONDS = float(SESSION_TTL_SECONDS)
"""How long a session nobody is attached to is kept: the lifetime of its relay row."""


@final
class SessionEnd:
    """The two ways a harness session ends (``claude-session-end.tex``).

    ``EndOwnSession``: a connection of the session's current run asks.
    ``ExpireClaude``: the session has had no connection for the retention.  Both
    run the same four effects, each idempotent, and drop the record only when
    all are done, so an end that fails part way leaves the record, and the next
    attempt finishes it: no row or tty name outlives its record.
    """

    __slots__ = ("_live",)

    _live: SessionTable

    def __new__(cls, live: SessionTable) -> Self:
        """End the sessions held in *live*."""
        self = super().__new__(cls)
        self._live = live
        return self

    async def end(
        self, session: Session, _verbs: SessionVerbs, p: Params
    ) -> CommandResult:
        """End *session* at the request of one of its connections.

        The caller has already been checked to be of the current run.

        Raises:
            RefusalError: *session* is a shell's session, which ends with
                ``session_end``.
        """
        p.done()
        key = SessionKey(session.session_id, CLAUDE_CODE_ROLE)
        if self._live.snapshot().get(key) is not session:
            msg = "end applies to a harness session; shell sessions use session_end"
            raise RefusalError.of(ErrorCode.INVALID_PARAMS, msg)
        async with self._live.guard(key):
            await self._finish(session)
            self._live.forget(key, spare=session)
        return CommandResult(text="Session ended.", json_data={"ended": True})

    async def expire(self, retention: float) -> None:
        """End every harness session that has had no connection for *retention*.

        A session that cannot be ended now (the relay is down) keeps its record
        and is tried again by the next sweep.
        """
        for key, session in self._live.snapshot().items():
            if key.role != CLI_ROLE and self._is_expired(session, retention):
                try:
                    await self._expire_one(key, session, retention)
                except RELAY_ERRORS:
                    logger.warning(
                        "could not expire session %s", key.session_id, exc_info=True
                    )

    @staticmethod
    def _is_expired(session: Session, retention: float) -> bool:
        return session.is_dormant and session.idle_for() >= retention

    async def _expire_one(
        self, key: SessionKey, session: Session, retention: float
    ) -> None:
        async with self._live.guard(key):
            # A hello may have reattached it while this waited for the guard.
            if self._live.snapshot().get(key) is session and self._is_expired(
                session, retention
            ):
                await self._finish(session)
                self._live.forget(key)

    @staticmethod
    async def refile_held(session: Session) -> None:
        """Give the relay back every message the session's outbox holds.

        A consuming relay removed that mail at fetch, so while it is held here it
        is out of reach of the drain that ends or expires the session.
        """
        inbox = Inbox(session.context, (), ())
        for message in session.outbox.held():
            if await inbox.refile(message):
                session.outbox.forget([message])

    async def _finish(self, session: Session) -> None:
        """Run the four effects of an end, then mark the session ended."""
        async with session.exclusive():
            await self.refile_held(session)
            ctx = session.context
            await InboxDrain(ctx.relay).move(ctx.session_key, ctx.user)
            owner = await ctx.relay.get_tty_reservation_owner(ctx.user, ctx.tty_name)
            if owner == ctx.session_key:  # a name another session took stays theirs
                await ctx.relay.release_tty_name(ctx.user, ctx.tty_name)
            await ctx.relay.delete_session(ctx.session_key)
            await self._logout(session)
            session.mark_ended()

    @staticmethod
    async def _logout(session: Session) -> None:
        """Record the logout; history must not stop an end."""
        ctx = session.context
        try:
            await ctx.relay.append_wtmp(
                SessionEvent(
                    session_key=ctx.session_key,
                    event="logout",
                    user=ctx.user,
                    tty=ctx.tty,
                    tty_name=ctx.tty_name,
                    hostname=get_hostname(),
                    pwd=session.cwd,
                    repo=ctx.config.repo_name,
                )
            )
        except RELAY_ERRORS:
            logger.warning(
                "could not record a logout for %s", ctx.session_key, exc_info=True
            )

    async def end_shell(
        self, session: Session, verbs: SessionVerbs, p: Params
    ) -> CommandResult:
        """File the mail a shell session holds back in the relay, end it, retire it.

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
                await self.refile_held(held)
            result = await verbs.end(target)
            if held is not None:
                held.mark_ended()
        row = cast("dict[str, str]", result.json_data)
        self._live.retire(row["id"], spare=session)
        return result
