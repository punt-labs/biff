"""Ending a harness session: on its own request, or when retention has run out."""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack, nullcontext
from typing import TYPE_CHECKING, Self, cast, final

from biff.commands._result import CommandResult
from biff.daemon.held_mail import HeldMail
from biff.daemon.mail_transfer import MailTransfer
from biff.daemon.refusal import RefusalError
from biff.daemon.session_key import SessionKey
from biff.frame_reply import ErrorCode
from biff.models import SessionEvent
from biff.relay import SESSION_TTL_SECONDS
from biff.relay_errors import RELAY_ERRORS
from biff.session_id import CLAUDE_CODE_ROLE, CLI_ROLE
from biff.tty import get_hostname

if TYPE_CHECKING:
    import uuid

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

    __slots__ = ("_live", "_running")

    _live: SessionTable
    _running: set[asyncio.Task[None]]

    def __new__(cls, live: SessionTable) -> Self:
        """End the sessions held in *live*."""
        self = super().__new__(cls)
        self._live = live
        self._running = set()
        return self

    async def end(
        self, session: Session, _verbs: SessionVerbs, p: Params
    ) -> CommandResult:
        """End *session* at the request of one of its connections.

        The caller has already been checked to be of the current run.  Ending a
        session that is already ended succeeds and does nothing more.

        Raises:
            RefusalError: *session* is a shell's session, which ends with
                ``session_end``.
        """
        p.done()
        if not session.is_agent:
            msg = "end applies to a harness session; shell sessions use session_end"
            raise RefusalError.of(ErrorCode.INVALID_PARAMS, msg)
        key = SessionKey(session.session_id, CLAUDE_CODE_ROLE)
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(self._live.guard(key))
            if not session.is_ended:  # another end may have finished while this waited
                try:
                    await session.begin_end()
                except BaseException:
                    session.abort_end()
                    raise
                # From here the effects run as a task of their own, holding the
                # guard: a caller that goes away part way (a hook killed by its
                # time limit) must not leave the row deleted and the tty name
                # still held.  Until here a caller that leaves leaves the session
                # open.
                work = asyncio.ensure_future(
                    self._complete(key, session, stack.pop_all())
                )
                self._running.add(work)
                work.add_done_callback(self._reap)
                await asyncio.shield(work)
        return CommandResult(text="Session ended.", json_data={"ended": True})

    async def _complete(
        self, key: SessionKey, session: Session, held: AsyncExitStack
    ) -> None:
        """Run the effects of the end, then drop the record; release the guard."""
        async with held:
            try:
                await self._finish(session)
            except BaseException:
                session.abort_end()
                raise
            self._live.forget(key, spare=session)

    def _reap(self, work: asyncio.Task[None]) -> None:
        """Forget a finished end, and log one that failed with no caller to tell."""
        self._running.discard(work)
        if not work.cancelled() and (exc := work.exception()) is not None:
            logger.warning("ending a session failed", exc_info=exc)

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

    async def _finish(self, session: Session) -> None:
        """Run the four effects of an end, then mark the session ended."""
        async with session.exclusive():
            ctx = session.context
            await self._forward(session)
            await ctx.relay.delete_session(ctx.session_key)  # no new mail routes here
            await self._forward(session)  # what was accepted while the row went
            # Only if this session still holds the name: one that changed hands
            # after the check is never freed.
            await ctx.relay.release_tty_name_if(ctx.user, ctx.tty_name, ctx.session_key)
            await self._logout(session)
            session.mark_ended()

    @staticmethod
    async def _forward(session: Session) -> None:
        """Move the session's mail, held and unread, into its user's mailbox.

        Mail a consuming relay gave up at fetch is held in the session's outbox
        until it is forwarded, so a relay that drops in between loses nothing: the
        next attempt forwards what is still held first.  Forwarding is by derived
        id, so a message sent twice is one.
        """
        ctx = session.context
        transfer = MailTransfer(ctx.relay)
        sent: set[uuid.UUID] = set()
        # A fetch returns a bounded batch, so go on until one brings nothing new.
        while fresh := [
            m
            for m in session.outbox.merge(await ctx.relay.fetch(ctx.session_key))
            if m.id not in sent
        ]:
            for message in fresh:
                await transfer.forward(ctx.session_key, ctx.user, message)
                session.outbox.forget([message])
                sent.add(message.id)

    @staticmethod
    async def _logout(session: Session) -> None:
        """Record the logout; a failure keeps the session for the next end."""
        ctx = session.context
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
                await HeldMail.refile(held)
            result = await verbs.end(target)
            if held is not None:
                held.mark_ended()
        row = cast("dict[str, str]", result.json_data)
        self._live.retire(row["id"], spare=session)
        return result
