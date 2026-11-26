"""Session records: one per session id, attached by any number of clients."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from biff.cli_context import CliContext
from biff.daemon.refusal import RefusalError
from biff.frames import ErrorCode
from biff.models import UserSession
from biff.nats_relay import NatsRelay
from biff.relay_errors import RELAY_ERRORS
from biff.session_id import CLAUDE_CODE_ROLE, CLI_ROLE
from biff.session_routing import SessionRouting
from biff.tty import claim_tty_name, get_hostname

if TYPE_CHECKING:
    from biff.daemon.relays import RelayTarget, ResolvedSession
    from biff.frames import Hello
    from biff.relay import Relay

__all__ = ["Attachment", "SessionStatus", "SessionTable"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionStatus:
    """What ``biff daemon status`` shows about one session."""

    session_id: str
    user: str
    attached: int
    run: str


@final
class Attachment:
    """One connection's hold on a session, for the length of the connection."""

    __slots__ = ("_run", "_session", "_subscription")

    _session: Session
    _run: str
    _subscription: asyncio.Task[None] | None

    def __new__(cls, session: Session, run: str) -> Self:
        """Attach to *session* as the run *run* ("" when the client claims none)."""
        self = super().__new__(cls)
        self._session = session
        self._run = run
        self._subscription = None
        return self

    @property
    def session(self) -> Session:
        """The session this connection is bound to, and no other."""
        return self._session

    @property
    def is_current(self) -> bool:
        """Whether this connection's run is still the session's run."""
        return self._run in ("", self._session.run)

    def replaced_by(self, run: str) -> bool:
        """Whether a hello for *run* replaces the run this connection claims."""
        return self._run not in ("", run)

    def hold_subscription(self, task: asyncio.Task[None]) -> bool:
        """Keep *task* as this connection's subscription; refuse a second one."""
        if self._subscription is not None:
            return False
        self._subscription = task
        return True

    def release_subscription(self) -> None:
        """Stop this connection's subscription, if it has one."""
        if self._subscription is not None:
            self._subscription.cancel()
            self._subscription = None

    def detach(self) -> None:
        """Release the connection's hold; the session stays, dormant if alone."""
        self.release_subscription()
        self._session.detach(self)


@final
class Session:
    """A session's record: identity, context and the connections attached to it.

    A dropped connection detaches; it never ends the session.  The relay row
    and the tty name outlive every connection.
    """

    __slots__ = (
        "_attachments",
        "_beat",
        "_context",
        "_interval",
        "_run",
        "_session_id",
        "_target",
    )

    _session_id: str
    _target: RelayTarget
    _context: CliContext
    _run: str
    _attachments: set[Attachment]
    _interval: float
    _beat: asyncio.Task[None] | None

    def __new__(
        cls,
        *,
        session_id: str,
        target: RelayTarget,
        context: CliContext,
        run: str,
        heartbeat_interval: float,
    ) -> Self:
        """Create the record of a registered session."""
        self = super().__new__(cls)
        self._session_id = session_id
        self._target = target
        self._context = context
        self._run = run
        self._attachments = set()
        self._interval = heartbeat_interval
        self._beat = None
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
        registered first) is kept and heartbeat; a missing one is created.
        """
        config = resolved.config
        role = CLI_ROLE if hello.client == "cli" else CLAUDE_CODE_ROLE
        routing = SessionRouting(hello.session_id, role=role)
        key = routing.session_key(config.user)
        row = await relay.get_session(key)
        if row is None:
            row = UserSession(
                user=config.user,
                tty=routing.routing_id,
                tty_name=await claim_tty_name(relay, config.user, key),
                hostname=get_hostname(),
                pwd=hello.cwd,
                display_name=config.display_name,
                kind=config.kind,
                launched_by=config.launched_by,
                owner_git=config.owner_git,
                repo=config.repo_name,
            )
            await relay.update_session(row)
        else:
            await relay.heartbeat(key)
        orgs = config.orgs
        org_repos = (
            await relay.discover_repos_for_orgs(orgs)
            if orgs and isinstance(relay, NatsRelay)
            else frozenset[str]()
        )
        context = CliContext(
            relay=relay,
            config=config,
            session_key=key,
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
    def status(self) -> SessionStatus:
        """The session as ``biff daemon status`` shows it."""
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

    def attach(self, run: str) -> Attachment:
        """Attach a connection claiming *run*, starting a new run if it differs.

        A new run releases the previous run's subscriptions and keeps every
        durable record; the replaced run's connections are refused from then
        on.  A client that claims no run neither replaces nor is replaced.
        """
        if run and run != self._run:
            if self._run:
                for held in self._attachments:
                    if held.replaced_by(run):
                        held.release_subscription()
            self._run = run
        attachment = Attachment(self, run)
        self._attachments.add(attachment)
        if self._beat is None:
            self._beat = asyncio.create_task(self._heartbeat())
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
            except RELAY_ERRORS:
                logger.info("heartbeat failed (transient); retrying", exc_info=True)


@final
class SessionTable:
    """Every session the daemon holds, get-or-create by session id."""

    __slots__ = ("_interval", "_locks", "_sessions")

    _interval: float
    _sessions: dict[str, Session]
    _locks: defaultdict[str, asyncio.Lock]

    def __new__(cls, *, heartbeat_interval: float) -> Self:
        """Create an empty table whose sessions heartbeat every interval."""
        self = super().__new__(cls)
        self._interval = heartbeat_interval
        self._sessions = {}
        self._locks = defaultdict(asyncio.Lock)
        return self

    @property
    def statuses(self) -> tuple[SessionStatus, ...]:
        """Every session, for ``biff daemon status``."""
        return tuple(s.status for s in self._sessions.values())

    async def open(
        self, hello: Hello, resolved: ResolvedSession, relay: Relay
    ) -> Attachment:
        """Attach the connection *hello* describes to its session.

        A second client for the same session id attaches to the existing
        record; the table never holds two records for one id, and an attach
        never changes another session's record.

        Raises:
            RefusalError: The session id belongs to another user or relay.
        """
        async with self._locks[hello.session_id]:
            session = self._sessions.get(hello.session_id)
            if session is None:
                session = await Session.register(
                    hello, resolved, relay, heartbeat_interval=self._interval
                )
                self._sessions[hello.session_id] = session
            else:
                session.check_same(resolved)
            return session.attach(hello.main_agent_id)
