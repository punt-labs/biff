"""CLI session lifecycle — unified session management for all CLI modes.

Provides ``cli_session()``, an async context manager that mirrors the
MCP server's ``_active_lifespan``.  Both the REPL (``biff`` with no
args) and inline commands (``biff who``) use this for a proper session::

    async with cli_session() as ctx:
        result = await commands.who(ctx)
        print(result.text)

An invocation attaches to a named session (see ``cli_session_host``) and
detaches on exit; the session, its relay row and its ``ttyN`` name outlive
the process, so consecutive commands from one shell share one address.
Interactive mode adds a heartbeat loop while attached.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from biff.cli_context import CliContext
from biff.cli_session_host import SessionHost
from biff.cli_session_select import SessionSelector
from biff.config import ResolvedConfig, load_cli_config
from biff.nats_relay import NatsRelay
from biff.relay_select import RelaySelector
from biff.tty import get_hostname, get_pwd

if TYPE_CHECKING:
    from biff.relay import Relay

logger = logging.getLogger(__name__)


async def _heartbeat_loop(
    relay: Relay,
    session_key: str,
    shutdown: asyncio.Event,
    *,
    interval: float = 60.0,
    renew: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Periodic heartbeat to keep the CLI session alive in KV.

    *renew* also extends the stored lease, so a long-running REPL is never
    mistaken for an abandoned session by another command's sweep.
    """
    while not shutdown.is_set():
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        try:
            await relay.heartbeat(session_key)
            if renew is not None:
                await renew()
        except asyncio.CancelledError:
            return
        except Exception:
            # INFO, not WARNING: a transient NATS timeout auto-recovers on the
            # next tick and must NOT dump a traceback into the interactive REPL.
            # The stderr handler floors at WARNING, so INFO stays off the
            # terminal while the file handler (INFO) keeps the full traceback
            # for diagnosis.
            logger.info("CLI heartbeat failed (transient); retrying", exc_info=True)


@dataclass(frozen=True)
class HostHandle:
    """An open relay with the session host and config built on it."""

    host: SessionHost
    resolved: ResolvedConfig
    relay: Relay

    async def org_repos(self) -> frozenset[str]:
        """Return the repos of the configured orgs (DES-034); only NATS knows them."""
        orgs = self.resolved.config.orgs
        if not (orgs and isinstance(self.relay, NatsRelay)):
            return frozenset[str]()
        found = await asyncio.gather(
            *(self.relay.discover_repos_for_org(org) for org in orgs)
        )
        return frozenset[str]().union(*found)


@asynccontextmanager
async def cli_session_host(
    *, user_override: str | None = None, start: Path | None = None
) -> AsyncGenerator[HostHandle]:
    """Open the relay and a :class:`SessionHost` without attaching to a session.

    Used by the ``session`` verbs, which manage sessions rather than run as
    one.  *start* picks the repo (default: the current directory's).
    """
    resolved = load_cli_config(user_override=user_override, start=start)
    config = resolved.config

    relay = RelaySelector(config, resolved.data_dir).select(dormant=False)
    try:
        host = SessionHost(
            relay=relay,
            config=config,
            selector=SessionSelector.from_resolved(resolved),
            hostname=get_hostname(),
            pwd=get_pwd(),
        )
        await host.sweep()
        yield HostHandle(host=host, resolved=resolved, relay=relay)
    finally:
        try:
            await relay.close()
        except Exception:
            logger.warning("Failed to close CLI relay", exc_info=True)


@asynccontextmanager
async def cli_session(
    *,
    interactive: bool = False,
    user_override: str | None = None,
    session_ref: str | None = None,
) -> AsyncGenerator[CliContext]:
    """Attach to a CLI session for the duration of one invocation.

    *session_ref* is the explicit ``-s`` / ``BIFF_SESSION`` value (a tty name
    or id); without it the session is the harness session, the repo's
    pointed session, or a new one.  On exit the heartbeat stops and the relay
    closes; the session's row and tty name are left in place.
    """
    shutdown = asyncio.Event()
    heartbeat_task: asyncio.Task[None] | None = None
    async with cli_session_host(user_override=user_override) as handle:
        config = handle.resolved.config
        try:
            attached = await handle.host.resolve(session_ref)
            session_key = attached.routing.session_key(config.user)

            org_repos = await handle.org_repos()

            if interactive:
                record = attached.record
                renew = (
                    None
                    if record is None
                    else functools.partial(handle.host.renew, record)
                )
                heartbeat_task = asyncio.create_task(
                    _heartbeat_loop(handle.relay, session_key, shutdown, renew=renew)
                )

            yield handle.host.context_for(
                attached,
                relay=handle.relay,
                org_repos=org_repos,
                repo_root=handle.resolved.repo_root,
            )
        finally:
            shutdown.set()
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task
