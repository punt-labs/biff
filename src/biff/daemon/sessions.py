"""Session records: one per session id, attached by any number of clients."""

from __future__ import annotations

import asyncio
import time
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from biff.cli_context import CliContext
from biff.daemon.attachment import Attachment
from biff.daemon.heartbeat import Heartbeat
from biff.daemon.orgs import OrgRepos
from biff.daemon.outbox import Outbox
from biff.daemon.presence import PresenceRow
from biff.daemon.refusal import RefusalError
from biff.daemon.run_ledger import RunLedger
from biff.daemon.session_hold import SessionHold
from biff.daemon.session_key import SessionKey
from biff.daemon.talk_channel import TalkChannel
from biff.daemon.tasks import Supervised
from biff.frame_reply import ErrorCode
from biff.session_id import CLAUDE_CODE_ROLE, CLI_ROLE, derive_routing_id
from biff.session_routing import SessionRouting

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from contextlib import AbstractAsyncContextManager

    from biff.daemon.relays import RelayPool, RelayTarget, ResolvedSession
    from biff.frame_client import Hello
    from biff.relay import Relay

__all__ = ["Session", "SessionStatus", "SessionTable"]

_TALK_CHECK_SECONDS = 2.0


@dataclass(frozen=True)
class SessionStatus:
    """What the daemon reports about one session."""

    session_id: str
    user: str
    attached: int
    run: str


@final
class Session:
    """A session's record: identity, context and the connections attached to it.

    A dropped connection detaches; it never ends the session.  The relay row
    and the tty name outlive every connection, and are recreated if they vanish
    while no one is attached.
    """

    __slots__ = (
        "_attachments",
        "_beat",
        "_context",
        "_cwd",
        "_hold",
        "_idle_since",
        "_is_ended",
        "_orgs",
        "_outbox",
        "_presence",
        "_pulse",
        "_runs",
        "_session_id",
        "_talk",
        "_target",
    )

    _session_id: str
    _cwd: str
    _target: RelayTarget
    _context: CliContext
    _presence: PresenceRow
    _orgs: OrgRepos
    _outbox: Outbox
    _talk: TalkChannel
    _runs: RunLedger
    _attachments: set[Attachment]
    _pulse: Heartbeat
    _beat: asyncio.Task[None] | None
    _is_ended: bool
    _idle_since: float
    _hold: SessionHold

    def __new__(
        cls,
        *,
        session_id: str,
        cwd: str,
        target: RelayTarget,
        context: CliContext,
        presence: PresenceRow,
        orgs: OrgRepos,
        run: str,
        heartbeat_interval: float,
    ) -> Self:
        """Create the record of a registered session."""
        self = super().__new__(cls)
        self._session_id = session_id
        self._cwd = cwd
        self._target = target
        self._context = context
        self._presence = presence
        self._orgs = orgs
        self._outbox = Outbox(retains=context.relay.consumes_on_fetch)
        self._talk = TalkChannel(
            relay=context.relay,
            session_key=context.session_key,
            talk=context.talk,
            interval=min(heartbeat_interval, _TALK_CHECK_SECONDS),
        )
        self._runs = RunLedger(run)
        self._attachments = set()
        self._pulse = Heartbeat(
            context.relay, context.session_key, session_id, heartbeat_interval
        )
        self._beat = None
        self._is_ended = False
        self._idle_since = time.monotonic()
        self._hold = SessionHold()
        return self

    @classmethod
    async def register(
        cls,
        hello: Hello,
        resolved: ResolvedSession,
        relay: Relay,
        *,
        heartbeat_interval: float,
        org_refresh_interval: float,
    ) -> Self:
        """Create the session *hello* names and make sure the relay lists it.

        An existing relay row (a daemon restart, or an in-process server that
        registered first) is kept and its liveness refreshed; a missing one is
        created.
        """
        config = resolved.config
        routing = SessionRouting(hello.session_id, role=SessionKey.of(hello).role)
        presence = PresenceRow(relay, config, routing, hello.cwd)
        row = await presence.ensure()
        orgs = OrgRepos(relay, config.orgs, org_refresh_interval)
        await orgs.refresh()
        context = CliContext(
            relay=relay,
            config=config,
            session_key=presence.key,
            user=config.user,
            tty=routing.routing_id,
            tty_name=row.tty_name,
            org_repos=orgs.current,
            repo_root=resolved.repo_root,
        )
        return cls(
            session_id=hello.session_id,
            cwd=hello.cwd,
            target=resolved.target,
            context=context,
            presence=presence,
            orgs=orgs,
            run=hello.main_agent_id,
            heartbeat_interval=heartbeat_interval,
        )

    @property
    def session_id(self) -> str:
        """The id the harness gave the session."""
        return self._session_id

    @property
    def cwd(self) -> str:
        """The directory the client that registered the session runs in."""
        return self._cwd

    @property
    def context(self) -> CliContext:
        """The context every command of this session runs in."""
        return self._context

    @property
    def run(self) -> str:
        """The main agent id of the session's current run."""
        return self._runs.current

    @property
    def outbox(self) -> Outbox:
        """Mail fetched for this session and not yet delivered."""
        return self._outbox

    @property
    def talk(self) -> TalkChannel:
        """The session's talk subscription, and the notices it raises."""
        return self._talk

    @property
    def is_dormant(self) -> bool:
        """Whether no connection is attached."""
        return not self._attachments

    def requests(self, method: str) -> AbstractAsyncContextManager[None]:
        """Hold the session open for one request, which ``end`` does not.

        An agent's session that is ended refuses it: the end deleted the row,
        which nothing may rebuild.  A shell's session is let through, its end being
        the shell's own command, which goes on.
        """
        return self._hold.requests(
            counted=method != "end", refuse=self.is_agent and self._is_ended
        )

    @property
    def is_agent(self) -> bool:
        """Whether this is an agent's session, not a shell's."""
        return self._context.tty == derive_routing_id(
            self._session_id, CLAUDE_CODE_ROLE
        )

    async def begin_end(self) -> None:
        """Refuse new requests, and wait for those running to finish."""
        await self._hold.begin_end()

    def abort_end(self) -> None:
        """Accept requests again: the end failed, and the session goes on."""
        self._hold.abort_end()

    def idle_for(self) -> float:
        """Return the seconds since the last connection left, or since the start."""
        return time.monotonic() - self._idle_since

    @property
    def status(self) -> SessionStatus:
        """The session as the daemon reports it."""
        return SessionStatus(
            session_id=self._session_id,
            user=self._context.user,
            attached=len(self._attachments),
            run=self.run,
        )

    def check_same(self, resolved: ResolvedSession) -> None:
        """Refuse a hello that would move this session to another user or relay.

        Raises:
            RefusalError: The hello names the same session id for another
                user or relay target.
        """
        if (
            resolved.config.user != self._context.user
            or resolved.target != self._target
        ):
            msg = (
                f"session {self._session_id} already belongs to "
                f"{self._context.user} on another relay; use a different session id"
            )
            raise RefusalError.of(ErrorCode.SESSION_CONFLICT, msg)

    async def revive(self) -> None:
        """Recreate the relay row if it vanished while no one was attached."""
        row = await self._presence.ensure()
        if row.tty_name != self._context.tty_name:
            self._context = self._context.with_tty_name(row.tty_name)

    async def touch(self) -> None:
        """Count a command from a shell as activity on the session's presence row."""
        await self._presence.touch()

    def exclusive(self) -> AbstractAsyncContextManager[None]:
        """Hold the session while mail moves in or out of its outbox."""
        return self._hold.exclusive()

    @property
    def queued(self) -> int:
        """How many callers hold :meth:`exclusive` or wait for it."""
        return self._hold.queued

    @property
    def is_ended(self) -> bool:
        """Whether the session was ended; it answers no more reads."""
        return self._is_ended

    def mark_ended(self) -> None:
        """Record that the session was ended."""
        self._is_ended = True

    def end_attachments(self) -> None:
        """Tell every connection attached that the session ended."""
        for held in tuple(self._attachments):
            held.end()

    def attach(self, run: str) -> Attachment:
        """Attach a connection claiming *run*, starting a new run if it differs.

        A new run releases the previous run's subscriptions, ends its
        connections, retires its id, and keeps every durable record.  A retired
        id is refused for good: a replaced run that obeys "reconnect" must start
        a new run, not evict the current one.  A client that claims no run
        neither replaces nor is replaced.

        Raises:
            RefusalError: *run* is the id of a run that was replaced.
        """
        if self._runs.admit(self._session_id, run):
            for held in tuple(self._attachments):
                if held.replaced_by(run):
                    held.replace()
        attachment = Attachment(self, run)
        self._attachments.add(attachment)
        self._talk.start()
        if self._beat is None or self._beat.done():
            self._beat = Supervised.spawn(
                self._pulse.run(self._refresh_orgs), f"heartbeat {self._session_id}"
            )
        return attachment

    def detach(self, attachment: Attachment) -> None:
        """Forget *attachment*; when no connection remains, stop heartbeating.

        The last client to leave also ends a talk it left open: the session's
        peer is told, and the session is idle for its next client.
        """
        self._attachments.discard(attachment)
        if not self._attachments:
            self._idle_since = time.monotonic()
            self._talk.stop()
            Supervised.spawn(self._hang_up(), f"talk hang-up {self._session_id}")
            if self._beat is not None:
                self._beat.cancel()
                self._beat = None

    async def _hang_up(self) -> None:
        """End the talk left open, unless a client attached in the meantime."""
        if self.is_dormant:
            await self._talk.hang_up(self._context)

    async def _refresh_orgs(self) -> None:
        """Follow the organization's repositories as the heartbeat finds them."""
        if await self._orgs.refresh():
            self._context = self._context.with_org_repos(self._orgs.current)


@final
class SessionTable:
    """Every session the daemon holds, get-or-create by session id and role."""

    __slots__ = ("_interval", "_locks", "_org_interval", "_sessions", "_users")

    _interval: float
    _org_interval: float
    _sessions: dict[SessionKey, Session]
    _locks: defaultdict[SessionKey, asyncio.Lock]
    _users: Counter[SessionKey]

    def __new__(cls, *, heartbeat_interval: float, org_refresh_interval: float) -> Self:
        """Create an empty table whose sessions run their timers at these intervals."""
        self = super().__new__(cls)
        self._interval = heartbeat_interval
        self._org_interval = org_refresh_interval
        self._sessions = {}
        self._locks = defaultdict(asyncio.Lock)
        self._users = Counter()
        return self

    @property
    def statuses(self) -> tuple[SessionStatus, ...]:
        """Every session the daemon holds."""
        return tuple(s.status for s in self._sessions.values())

    async def open(
        self, hello: Hello, resolved: ResolvedSession, relays: RelayPool
    ) -> Attachment:
        """Attach the connection *hello* describes to its session.

        A second client of the same session id and role attaches to the existing
        record; the table never holds two records for one key, and an attach
        never changes another session's record.  A shell and an agent that share
        a session id are two sessions.  A relay is acquired only for a
        session that is new, so a refused hello opens no connection.

        Raises:
            RefusalError: The session id belongs to another user or relay.
        """
        key = SessionKey.of(hello)
        async with self.guard(key):
            session = self._sessions.get(key)
            if session is None:
                session = await Session.register(
                    hello,
                    resolved,
                    relays.acquire(resolved),
                    heartbeat_interval=self._interval,
                    org_refresh_interval=self._org_interval,
                )
                self._sessions[key] = session
            else:
                session.check_same(resolved)
                if session.is_dormant:
                    await session.revive()
            if hello.client == "cli":
                await session.touch()
            return session.attach(hello.main_agent_id)

    def holding(self, session_id: str) -> Session | None:
        """Return the shell session *session_id* if the daemon holds it."""
        return self._sessions.get(SessionKey(session_id, CLI_ROLE))

    def retire(self, session_id: str, *, spare: Session | None = None) -> None:
        """Forget the shell session *session_id*, which was ended."""
        self.forget(SessionKey(session_id, CLI_ROLE), spare=spare)

    def forget(self, key: SessionKey, *, spare: Session | None = None) -> None:
        """Drop the session kept under *key*, which was ended.

        Every connection attached to it is told, except those of *spare*, the
        session that asked: its connection ends with its command, after the
        reply.
        """
        session = self._sessions.pop(key, None)
        if self._users[key] == 0:
            self._locks.pop(key, None)  # guard drops it only while no session
        if session is not None and session is not spare:
            session.end_attachments()

    def snapshot(self) -> dict[SessionKey, Session]:
        """Return a copy of every session held, safe to iterate while awaiting."""
        return dict(self._sessions)

    @asynccontextmanager
    async def guard(self, key: SessionKey) -> AsyncGenerator[None]:
        """Serialize work on one session key, dropping its lock once it is idle."""
        self._users[key] += 1
        try:
            async with self._locks[key]:
                yield
        finally:
            self._users[key] -= 1
            if self._users[key] == 0:
                del self._users[key]
                if key not in self._sessions:
                    del self._locks[key]
