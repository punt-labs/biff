"""CLI session lifecycle — unified session management for all CLI modes.

Provides ``cli_session()``, an async context manager that mirrors the
MCP server's ``_active_lifespan``.  Both the REPL (``biff`` with no
args) and inline commands (``biff who``) use this for a proper session::

    async with cli_session() as ctx:
        result = await commands.who(ctx)
        print(result.text)

The session is registered in KV on entry, assigned a ``ttyN`` name,
writes a wtmp login event, and is cleaned up on exit (KV delete,
wtmp logout).  Interactive mode adds a heartbeat loop.

This replaces the old pseudo-ephemeral session with a 5-minute TTL.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime

from biff.config import load_cli_config
from biff.models import BiffConfig, SessionEvent, UserSession
from biff.nats_relay import NatsRelay
from biff.relay import Relay
from biff.talk_state import TalkState
from biff.tty import (
    build_session_key,
    claim_tty_name,
    generate_tty,
    get_hostname,
    get_pwd,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CliContext:
    """Context for a CLI command: relay connection + identity."""

    relay: Relay
    config: BiffConfig
    session_key: str
    user: str
    tty: str
    tty_name: str = ""
    org_repos: frozenset[str] = frozenset()
    talk: TalkState = field(init=False)

    def __post_init__(self) -> None:
        """Compose the shared ephemeral talk state from this session's identity.

        ``TalkState`` is mutable and held by reference inside the frozen
        context — the same seating ``ServerState`` gives its
        ``ActivityTracker``.  Both front-ends feed it every talk
        notification and drain it in their own idiom (talk_state.py).
        """
        object.__setattr__(
            self,
            "talk",
            TalkState(
                relay=self.relay,
                user=self.user,
                tty=self.tty,
                session_key=self.session_key,
                tty_name=self.tty_name,
            ),
        )

    @property
    def visible_repos(self) -> frozenset[str]:
        """All repos visible: self + explicit peers + org-discovered (DES-034)."""
        if not self.org_repos:
            return self.config.visible_repos
        return self.config.visible_repos | self.org_repos


async def _heartbeat_loop(
    relay: Relay, session_key: str, shutdown: asyncio.Event, *, interval: float = 60.0
) -> None:
    """Periodic heartbeat to keep the CLI session alive in KV."""
    while not shutdown.is_set():
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        try:
            await relay.heartbeat(session_key)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            # INFO, not WARNING: a transient NATS timeout auto-recovers on the
            # next tick and must NOT dump a traceback into the interactive REPL.
            # The stderr handler floors at WARNING, so INFO stays off the
            # terminal while the file handler (INFO) keeps the full traceback
            # for diagnosis.
            logger.info("CLI heartbeat failed (transient); retrying", exc_info=True)


async def _cli_session_cleanup(
    relay: NatsRelay,
    *,
    user: str,
    tty: str,
    tty_name: str,
    session_key: str,
    session: UserSession | None,
    registered: bool,
    name_reserved: bool,
    shutdown: asyncio.Event,
    heartbeat_task: asyncio.Task[None] | None,
    repo_name: str,
) -> None:
    """Clean up a CLI session: stop heartbeat, write logout, release name, close."""
    shutdown.set()
    if heartbeat_task is not None:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task

    # Release TTY name reservation regardless of session registration state.
    # claim_tty_name may succeed (writing a reservation) before update_session
    # fails, so we must track reservation state independently.
    if name_reserved and tty_name and not registered:
        try:
            await relay.release_tty_name(user, tty_name)
        except Exception:  # noqa: BLE001
            logger.warning("Failed to release TTY name %s", tty_name, exc_info=True)

    if registered and session is not None:
        logout_event = SessionEvent(
            session_key=session_key,
            event="logout",
            user=user,
            tty=tty,
            tty_name=tty_name,
            hostname=session.hostname,
            pwd=session.pwd,
            timestamp=datetime.now(UTC),
            repo=repo_name,
        )
        try:
            await relay.append_wtmp(logout_event)
            await relay.flush()
        except Exception:  # noqa: BLE001
            logger.warning("Failed to write CLI wtmp logout", exc_info=True)

        # Release TTY name reservation (DES-035).
        if tty_name:
            try:
                await relay.release_tty_name(user, tty_name)
            except Exception:  # noqa: BLE001
                logger.warning("Failed to release TTY name %s", tty_name, exc_info=True)

        try:
            await relay.delete_session(session_key)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to delete CLI session %s", session_key, exc_info=True
            )

    try:
        await relay.close()
    except Exception:  # noqa: BLE001
        logger.warning("Failed to close CLI relay", exc_info=True)


@asynccontextmanager
async def cli_session(
    *, interactive: bool = False, user_override: str | None = None
) -> AsyncIterator[CliContext]:
    """Provide a NATS relay + session with proper lifecycle.

    On entry: connect, register session (KV), auto-assign ttyN,
    write wtmp login event.  If *interactive*, start a heartbeat loop.

    On exit: write wtmp logout event, delete session (KV), disconnect.
    """
    resolved = load_cli_config(user_override=user_override)
    config = resolved.config

    if not config.relay_url:
        msg = (
            "CLI commands require a NATS relay. "
            "Configure relay.url in .punt-labs/biff/config.yaml "
            "or .punt-labs/biff/config.local.yaml"
        )
        raise ValueError(msg)

    relay = NatsRelay(
        url=config.relay_url,
        auth=config.relay_auth,
        name=f"biff-cli-{config.user}",
        repo_name=config.repo_name,
    )

    user = config.user
    tty = generate_tty()
    session_key = build_session_key(user, tty)
    tty_name = ""
    session: UserSession | None = None
    registered = False
    name_reserved = False
    shutdown = asyncio.Event()
    heartbeat_task: asyncio.Task[None] | None = None
    ctx: CliContext | None = None

    try:
        # Register session and auto-assign ttyN name (DES-035).
        tty_name = await claim_tty_name(relay, user, session_key)
        name_reserved = True

        # Org discovery (DES-034): discover repos for configured orgs.
        org_repos = frozenset[str]()
        if config.orgs:
            org_results: list[frozenset[str]] = list(
                await asyncio.gather(
                    *(relay.discover_repos_for_org(org) for org in config.orgs)
                )
            )
            org_repos = frozenset[str]().union(*org_results)

        session = UserSession(
            user=user,
            tty=tty,
            tty_name=tty_name,
            hostname=get_hostname(),
            pwd=get_pwd(),
            repo=config.repo_name,
            kind=config.kind,
        )
        await relay.update_session(session)
        registered = True

        # Write wtmp login event.
        login_event = SessionEvent(
            session_key=session_key,
            event="login",
            user=user,
            tty=tty,
            tty_name=tty_name,
            hostname=session.hostname,
            pwd=session.pwd,
            timestamp=datetime.now(UTC),
            repo=config.repo_name,
        )
        try:
            await relay.append_wtmp(login_event)
        except Exception:  # noqa: BLE001
            logger.warning("Failed to write CLI wtmp login", exc_info=True)

        if interactive:
            heartbeat_task = asyncio.create_task(
                _heartbeat_loop(relay, session_key, shutdown)
            )

        ctx = CliContext(
            relay=relay,
            config=config,
            session_key=session_key,
            user=user,
            tty=tty,
            tty_name=tty_name,
            org_repos=org_repos,
        )

        yield ctx
    finally:
        # The tty command may have renamed the session (updating the
        # frozen CliContext via object.__setattr__).  Use ctx.tty_name
        # when available so cleanup releases the CURRENT name, not the
        # stale original captured in the local ``tty_name``.
        final_tty_name = ctx.tty_name if ctx is not None else tty_name
        await _cli_session_cleanup(
            relay,
            user=user,
            tty=tty,
            tty_name=final_tty_name,
            session_key=session_key,
            session=session,
            registered=registered,
            name_reserved=name_reserved,
            shutdown=shutdown,
            heartbeat_task=heartbeat_task,
            repo_name=config.repo_name,
        )
