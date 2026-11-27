"""Session records: one per session id, attached by any number of clients."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Self, final

from biff.cli_context import CliContext
from biff.daemon.attachment import Attachment
from biff.daemon.orgs import OrgRepos
from biff.daemon.outbox import Outbox
from biff.daemon.presence import PresenceRow
from biff.daemon.refusal import RefusalError
from biff.daemon.session_key import SessionKey
from biff.daemon.tasks import Supervised
from biff.frame_reply import ErrorCode
from biff.relay_errors import RELAY_ERRORS, RelayLog
from biff.session_routing import SessionRouting

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from biff.daemon.relays import RelayPool, RelayTarget, ResolvedSession
    from biff.frame_client import Hello
    from biff.relay import Relay

__all__ = ["Session", "SessionStatus", "SessionTable"]

logger = logging.getLogger(__name__)

_MAX_RETIRED_RUNS = 64


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
        "_interval",
        "_log",
        "_orgs",
        "_outbox",
        "_presence",
        "_retired",
        "_run",
        "_session_id",
        "_target",
    )

    _session_id: str
    _target: RelayTarget
    _context: CliContext
    _presence: PresenceRow
    _orgs: OrgRepos
    _outbox: Outbox
    _run: str
    _retired: dict[str, None]
    _attachments: set[Attachment]
    _interval: float
    _beat: asyncio.Task[None] | None
    _log: RelayLog

    def __new__(
        cls,
        *,
        session_id: str,
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
        self._target = target
        self._context = context
        self._presence = presence
        self._orgs = orgs
        self._outbox = Outbox(retains=context.relay.consumes_on_fetch)
        self._run = run
        self._retired = {}
        self._attachments = set()
        self._interval = heartbeat_interval
        self._beat = None
        self._log = RelayLog(logger)
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
    def context(self) -> CliContext:
        """The context every command of this session runs in."""
        return self._context

    @property
    def run(self) -> str:
        """The main agent id of the session's current run."""
        return self._run

    @property
    def outbox(self) -> Outbox:
        """Mail fetched for this session and not yet delivered."""
        return self._outbox

    @property
    def is_dormant(self) -> bool:
        """Whether no connection is attached."""
        return not self._attachments

    @property
    def status(self) -> SessionStatus:
        """The session as the daemon reports it."""
        return SessionStatus(
            session_id=self._session_id,
            user=self._context.user,
            attached=len(self._attachments),
            run=self._run,
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
            self._context = replace(self._context, tty_name=row.tty_name)

    async def touch(self) -> None:
        """Count a command from a shell as activity on the session's presence row."""
        await self._presence.touch()

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
        if run in self._retired:
            msg = (
                f"run {run} of session {self._session_id} was replaced by a newer "
                "run and cannot come back; start a new run with a fresh run id"
            )
            raise RefusalError.of(ErrorCode.SESSION_REPLACED, msg)
        if run and run != self._run:
            if self._run:
                self._retire(self._run)
                for held in tuple(self._attachments):
                    if held.replaced_by(run):
                        held.replace()
            self._run = run
        attachment = Attachment(self, run)
        self._attachments.add(attachment)
        if self._beat is None or self._beat.done():
            self._beat = Supervised.spawn(
                self._heartbeat(), f"heartbeat {self._session_id}"
            )
        return attachment

    def _retire(self, run: str) -> None:
        """Remember *run* as replaced; only the most recent ones are kept."""
        self._retired[run] = None
        while len(self._retired) > _MAX_RETIRED_RUNS:
            del self._retired[next(iter(self._retired))]

    def detach(self, attachment: Attachment) -> None:
        """Forget *attachment*; stop heartbeating when no connection remains."""
        self._attachments.discard(attachment)
        if not self._attachments and self._beat is not None:
            self._beat.cancel()
            self._beat = None

    async def _heartbeat(self) -> None:
        """Send the session's one presence heartbeat for as long as it is attached."""
        while True:
            await asyncio.sleep(self._interval)
            ctx = self._context
            try:
                await ctx.relay.heartbeat(ctx.session_key)
            except RELAY_ERRORS as exc:
                self._log.failed("heartbeat", exc)
            else:
                self._log.recovered("heartbeat")
            if await self._orgs.refresh():
                self._context = replace(self._context, org_repos=self._orgs.current)


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
        async with self._guard(key):
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

    @asynccontextmanager
    async def _guard(self, key: SessionKey) -> AsyncGenerator[None]:
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
