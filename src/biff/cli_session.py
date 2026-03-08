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
from dataclasses import dataclass
from datetime import UTC, datetime

from biff.config import load_config
from biff.models import BiffConfig, SessionEvent, UserSession
from biff.nats_relay import NatsRelay
from biff.relay import Relay
from biff.tty import (
    assign_unique_tty_name,
    build_session_key,
    generate_tty,
    get_hostname,
    get_pwd,
    verify_tty_name,
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
            logger.warning("CLI heartbeat failed", exc_info=True)


@asynccontextmanager
async def cli_session(*, interactive: bool = False) -> AsyncIterator[CliContext]:
    """Provide a NATS relay + session with proper lifecycle.

    On entry: connect, register session (KV), auto-assign ttyN,
    write wtmp login event.  If *interactive*, start a heartbeat loop.

    On exit: write wtmp logout event, delete session (KV), disconnect.
    """
    resolved = load_config()
    config = resolved.config

    if not config.relay_url:
        msg = "CLI commands require a NATS relay. Configure relay_url in .biff."
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
    shutdown = asyncio.Event()
    heartbeat_task: asyncio.Task[None] | None = None

    try:
        # Register session and auto-assign ttyN name.
        tty_name = await assign_unique_tty_name(relay, session_key)

        session = UserSession(
            user=user,
            tty=tty,
            tty_name=tty_name,
            hostname=get_hostname(),
            pwd=get_pwd(),
        )
        await relay.update_session(session)
        registered = True

        # Verify no duplicate after write (closes TOCTOU window).
        tty_name = await verify_tty_name(relay, session_key, tty_name)

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
        )

        yield ctx
    finally:
        # Stop heartbeat.
        shutdown.set()
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task

        # Write wtmp logout event (only if session was registered).
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
            )
            try:
                await relay.append_wtmp(logout_event)
                await relay.flush()
            except Exception:  # noqa: BLE001
                logger.warning("Failed to write CLI wtmp logout", exc_info=True)

            # Delete session from KV.
            try:
                await relay.delete_session(session_key)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Failed to delete CLI session %s",
                    session_key,
                    exc_info=True,
                )

        try:
            await relay.close()
        except Exception:  # noqa: BLE001
            logger.warning("Failed to close CLI relay", exc_info=True)
