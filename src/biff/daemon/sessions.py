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
from biff.daemon.presence import PresenceRow
from biff.daemon.refusal import RefusalError
from biff.daemon.tasks import Supervised
from biff.frame_reply import ErrorCode
from biff.nats_relay import NatsRelay
from biff.relay_errors import RELAY_ERRORS, RelayLog
from biff.session_id import CLAUDE_CODE_ROLE, CLI_ROLE
from biff.session_routing import SessionRouting

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from biff.daemon.relays import RelayPool, RelayTarget, ResolvedSession
    from biff.frame_client import Hello
    from biff.relay import Relay

__all__ = ["Session", "SessionStatus", "SessionTable"]

logger = logging.getLogger(__name__)


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
        "_presence",
        "_run",
        "_session_id",
        "_target",
    )

    _session_id: str
    _target: RelayTarget
    _context: CliContext
    _presence: PresenceRow
    _run: str
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
        run: str,
        heartbeat_interval: float,
    ) -> Self:
        """Create the record of a registered session."""
        self = super().__new__(cls)
        self._session_id = session_id
        self._target = target
        self._context = context
        self._presence = presence
        self._run = run
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
    ) -> Self:
        """Create the session *hello* names and make sure the relay lists it.

        An existing relay row (a daemon restart, or an in-process server that
        registered first) is kept and its liveness refreshed; a missing one is
        created.
        """
        config = resolved.config
        role = CLI_ROLE if hello.client == "cli" else CLAUDE_CODE_ROLE
        routing = SessionRouting(hello.session_id, role=role)
        presence = PresenceRow(relay, config, routing, hello.cwd)
        row = await presence.ensure()
        orgs = config.orgs
        org_repos = (
            await relay.discover_repos_for_orgs(orgs)
            if orgs and isinstance(relay, NatsRelay)
            else frozenset[str]()
        )
        context = CliContext(
            relay=relay,
            config=config,
            session_key=presence.key,
            user=config.user,
            tty=routing.routing_id,
            tty_name=row.tty_name,
            org_repos=org_repos,
            repo_root=resolved.repo_root,
        )
        return cls(
            session_id=hello.session_id,
            target=resolved.target,
            context=context,
            presence=presence,
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

    def attach(self, run: str) -> Attachment:
        """Attach a connection claiming *run*, starting a new run if it differs.

        A new run releases the previous run's subscriptions, ends its
        connections, and keeps every durable record.  A client that claims no
        run neither replaces nor is replaced.
        """
        if run and run != self._run:
            if self._run:
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

    def detach(self, attachment: Attachment) -> None:
        """Forget *attachment*; stop heartbeating when no connection remains."""
        self._attachments.discard(attachment)
        if not self._attachments and self._beat is not None:
            self._beat.cancel()
            self._beat = None

    async def _heartbeat(self) -> None:
        """Send the session's one presence heartbeat for as long as it is attached."""
        ctx = self._context
        while True:
            await asyncio.sleep(self._interval)
            try:
                await ctx.relay.heartbeat(ctx.session_key)
            except RELAY_ERRORS as exc:
                self._log.failed("heartbeat", exc)
            else:
                self._log.recovered("heartbeat")


@final
class SessionTable:
    """Every session the daemon holds, get-or-create by session id."""

    __slots__ = ("_interval", "_locks", "_sessions", "_users")

    _interval: float
    _sessions: dict[str, Session]
    _locks: defaultdict[str, asyncio.Lock]
    _users: Counter[str]

    def __new__(cls, *, heartbeat_interval: float) -> Self:
        """Create an empty table whose sessions heartbeat every interval."""
        self = super().__new__(cls)
        self._interval = heartbeat_interval
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

        A second client for the same session id attaches to the existing
        record; the table never holds two records for one id, and an attach
        never changes another session's record.  A relay is acquired only for a
        session that is new, so a refused hello opens no connection.

        Raises:
            RefusalError: The session id belongs to another user or relay.
        """
        async with self._guard(hello.session_id):
            session = self._sessions.get(hello.session_id)
            if session is None:
                session = await Session.register(
                    hello,
                    resolved,
                    relays.acquire(resolved),
                    heartbeat_interval=self._interval,
                )
                self._sessions[hello.session_id] = session
            else:
                session.check_same(resolved)
                if session.is_dormant:
                    await session.revive()
            return session.attach(hello.main_agent_id)

    @asynccontextmanager
    async def _guard(self, session_id: str) -> AsyncGenerator[None]:
        """Serialize work on one session id, dropping its lock once it is idle."""
        self._users[session_id] += 1
        try:
            async with self._locks[session_id]:
                yield
        finally:
            self._users[session_id] -= 1
            if self._users[session_id] == 0:
                del self._users[session_id]
                if session_id not in self._sessions:
                    del self._locks[session_id]
