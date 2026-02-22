"""FastMCP application factory.

``create_server`` builds a fully configured FastMCP instance with all
tools registered. The returned server is run via ``mcp.run(transport=...)``.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastmcp import FastMCP

from biff.models import SessionEvent, UserSession
from biff.nats_relay import NatsRelay
from biff.relay import LocalRelay
from biff.server.state import ServerState
from biff.server.tools import register_all_tools
from biff.server.tools._descriptions import (
    poll_inbox,
    refresh_read_messages,
    set_tty_name,
)
from biff.server.tools._session import update_current_session
from biff.server.tools.tty import next_tty_name

logger = logging.getLogger(__name__)


def _sentinel_dir(repo_name: str) -> Path:
    """Sentinel directory for a repo: ``~/.biff/sentinels/{repo_name}/``."""
    return Path.home() / ".biff" / "sentinels" / repo_name


def _write_sentinel(repo_name: str, session_key: str) -> None:
    """Create a sentinel file marking a session for removal.

    Relay-agnostic — writes to ``~/.biff/sentinels/{repo}/`` so that
    any running server's reaper task can process it.  Safe to call
    from signal handlers (sync I/O only).
    """
    d = _sentinel_dir(repo_name)
    d.mkdir(parents=True, exist_ok=True)
    safe = session_key.replace(":", "-")
    (d / safe).write_text(session_key)


async def _reap_sentinels(state: ServerState) -> None:
    """Process sentinel files, deleting flagged sessions via the relay.

    Reads each file in the sentinel directory, calls
    ``relay.delete_session()`` (async — works for both NATS and local),
    and removes the sentinel.  Errors on individual sentinels are
    logged but don't prevent processing of others.
    """
    d = _sentinel_dir(state.config.repo_name)
    if not d.exists():
        return
    for sentinel in d.iterdir():
        if not sentinel.is_file():
            continue
        try:
            session_key = sentinel.read_text().strip()
        except OSError:
            continue
        try:
            await state.relay.delete_session(session_key)
        except Exception:  # noqa: BLE001 — relay errors vary by backend
            logger.warning("Failed to reap sentinel for %s", session_key, exc_info=True)
            continue
        sentinel.unlink(missing_ok=True)


async def _reap_loop(
    state: ServerState, shutdown: asyncio.Event, *, interval: float = 2.0
) -> None:
    """Background task: reap shutdown sentinels every *interval* seconds."""
    while not shutdown.is_set():
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
            return  # Shutdown requested
        except TimeoutError:
            pass
        await _reap_sentinels(state)


async def _heartbeat_loop(
    state: ServerState, shutdown: asyncio.Event, *, interval: float = 60.0
) -> None:
    """Periodic heartbeat to keep this session alive in the relay.

    Each ``heartbeat()`` call updates ``last_active`` and — for NATS KV —
    resets the key's TTL.  When the process sleeps (laptop lid closed) or
    dies (SIGKILL), heartbeats stop and the relay eventually expires the
    session.
    """
    while not shutdown.is_set():
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
            return  # Shutdown requested
        except TimeoutError:
            pass
        try:
            await state.relay.heartbeat(state.session_key)
        except Exception:  # noqa: BLE001 — relay errors vary by backend
            logger.warning("Heartbeat failed", exc_info=True)


def _kv_key_to_session_key(kv_key: str) -> str | None:
    """Convert a KV key (``user.tty``) to a session key (``user:tty``).

    Returns ``None`` if the key format is unexpected.
    """
    parts = kv_key.split(".", maxsplit=1)
    if len(parts) != 2:
        return None
    return f"{parts[0]}:{parts[1]}"


def _build_logout_event(session_key: str, cached: UserSession) -> SessionEvent:
    """Build a logout ``SessionEvent`` from a cached session."""
    return SessionEvent(
        session_key=session_key,
        event="logout",
        user=cached.user,
        tty=cached.tty,
        tty_name=cached.tty_name,
        hostname=cached.hostname,
        pwd=cached.pwd,
        timestamp=cached.last_active,
        plan=cached.plan,
    )


async def _wtmp_watcher_loop(state: ServerState, shutdown: asyncio.Event) -> None:
    """Watch KV session changes and append logout events to wtmp.

    Handles logout events for *other* sessions that disappear via TTL
    expiry or crash (no graceful shutdown).  Our own session's logout
    is written explicitly by ``_append_logout_event()`` during graceful
    shutdown, so the watcher skips ``state.session_key``.

    On PUT events, caches the session data so that logout events can
    include the ``last_active`` timestamp.

    **Duplicate risk**: If multiple servers are running, each may
    observe the same KV delete and write a logout event.  This is
    acceptable — ``/last`` pairs events by session_key, so extra
    logouts are harmless (they won't match a login).
    """
    relay = state.relay
    if not isinstance(relay, NatsRelay):
        return  # LocalRelay does not support wtmp
    if not relay.wtmp_available:
        return  # Wtmp stream not provisioned (account limit reached)

    kv = await relay.get_kv()
    cache: dict[str, UserSession] = {}

    try:
        watcher = await kv.watchall()  # pyright: ignore[reportUnknownMemberType]
        async for entry in watcher:  # pyright: ignore[reportUnknownVariableType]
            if shutdown.is_set():
                return
            await _handle_kv_entry(entry, relay, state, cache)
    except asyncio.CancelledError:
        return
    except Exception:  # noqa: BLE001
        logger.warning("Wtmp watcher loop exited with error", exc_info=True)


async def _handle_kv_entry(
    entry: object,  # nats KeyValue.Entry (untyped)
    relay: NatsRelay,
    state: ServerState,
    cache: dict[str, UserSession],
) -> None:
    """Route a single KV watch entry to the appropriate handler."""
    key = str(entry.key)  # type: ignore[attr-defined]  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType,reportAttributeAccessIssue]
    session_key = _kv_key_to_session_key(key)
    if session_key is None:
        return

    op = entry.operation  # type: ignore[attr-defined]  # pyright: ignore[reportUnknownMemberType,reportAttributeAccessIssue]
    val = entry.value  # type: ignore[attr-defined]  # pyright: ignore[reportUnknownMemberType,reportAttributeAccessIssue]
    if op is None and val is not None:
        # PUT — cache the session data
        try:
            session = UserSession.model_validate_json(
                val  # pyright: ignore[reportUnknownArgumentType]
            )
            cache[session_key] = session
        except Exception:  # noqa: BLE001
            logger.debug("Failed to parse KV entry %s", key)
    elif op in ("DEL", "PURGE"):
        await _handle_kv_delete(relay, state, cache, session_key)


async def _handle_kv_delete(
    relay: NatsRelay,
    state: ServerState,
    cache: dict[str, UserSession],
    session_key: str,
) -> None:
    """Handle a KV delete event — append logout for crashed/expired sessions.

    Skips our own session key because graceful shutdown writes the
    logout event explicitly in ``_append_logout_event()`` before the
    watcher is stopped.  For *other* sessions (TTL expiry, crash),
    any running server can write the logout on their behalf.
    """
    if session_key == state.session_key:
        return  # Our own shutdown writes logout explicitly
    cached = cache.pop(session_key, None)
    if cached is None:
        logger.debug(
            "No cached session for %s on DEL, skipping wtmp",
            session_key,
        )
        return
    try:
        await relay.append_wtmp(_build_logout_event(session_key, cached))
    except Exception:  # noqa: BLE001
        logger.warning(
            "Failed to append wtmp logout for %s",
            session_key,
            exc_info=True,
        )


async def _shutdown_tasks(
    shutdown: asyncio.Event, tasks: list[asyncio.Task[None]], *, timeout: float = 5.0
) -> None:
    """Stop background tasks cooperatively, falling back to hard cancel.

    Sets the *shutdown* event so tasks exit cleanly between iterations
    (avoids cancelling mid-NATS-I/O which corrupts shared connections).
    Waits up to *timeout* seconds for all tasks to finish, then
    force-cancels any stragglers.  Suppresses ``CancelledError`` at
    every ``await`` because this runs inside a ``finally`` block that
    may itself be responding to cancellation.
    """
    shutdown.set()
    with suppress(asyncio.CancelledError):
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout,
        )
    for t in tasks:
        if not t.done():
            t.cancel()
    for t in tasks:
        with suppress(asyncio.CancelledError):
            await t


async def _append_login_event(state: ServerState, tty_name: str) -> None:
    """Append a login event to wtmp for the current session."""
    session = await state.relay.get_session(state.session_key)
    if session is None:
        return
    login_event = SessionEvent(
        session_key=state.session_key,
        event="login",
        user=state.config.user,
        tty=state.tty,
        tty_name=tty_name,
        hostname=state.hostname,
        pwd=state.pwd,
        timestamp=session.last_active,
        plan=session.plan,
    )
    try:
        await state.relay.append_wtmp(login_event)
    except Exception:  # noqa: BLE001
        logger.warning("Failed to append wtmp login event", exc_info=True)


async def _append_logout_event(state: ServerState) -> None:
    """Append a logout event to wtmp for the current session.

    Called during graceful shutdown, *before* ``delete_session()``.
    The KV watcher cannot observe our own session deletion because it
    is stopped first, so logout must be written explicitly here.
    """
    session = await state.relay.get_session(state.session_key)
    if session is None:
        return
    try:
        await state.relay.append_wtmp(_build_logout_event(state.session_key, session))
    except Exception:  # noqa: BLE001
        logger.warning("Failed to append wtmp logout event", exc_info=True)


def create_server(state: ServerState) -> FastMCP[ServerState]:
    """Create a FastMCP server with all biff tools registered.

    The returned server is ready to run via ``mcp.run(transport=...)``.
    Starts a background inbox poller that keeps the tool description
    and status file in sync with incoming messages.
    """

    @asynccontextmanager
    async def lifespan(mcp: FastMCP[ServerState]) -> AsyncIterator[ServerState]:
        _cleaned_up = False

        def _signal_handler(_signum: int, _frame: object) -> None:
            nonlocal _cleaned_up
            if _cleaned_up:
                return
            _cleaned_up = True
            # Write sentinel — relay-agnostic, picked up by any
            # running server's reaper task.  Smallest possible
            # operation (touch a file), runs first.
            with suppress(OSError):
                _write_sentinel(state.config.repo_name, state.session_key)
            # Best-effort sync cleanup for LocalRelay only.
            if isinstance(state.relay, LocalRelay):
                with suppress(OSError):
                    state.relay.write_remove_sentinel(state.session_key)
                with suppress(OSError, ValueError):
                    state.relay.delete_session_sync(state.session_key)

        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, _signal_handler)

        # Auto-assign a ttyN name so the status bar always has identity.
        sessions = await state.relay.get_sessions()
        existing = [s.tty_name for s in sessions if s.tty_name]
        auto_name = next_tty_name(existing)
        set_tty_name(auto_name)
        await update_current_session(state, tty_name=auto_name)

        # Write the initial unread file immediately so the status line
        # has identity from the first render (before the poller ticks).
        await refresh_read_messages(mcp, state)

        await _append_login_event(state, auto_name)

        shutdown = asyncio.Event()
        poller = asyncio.create_task(poll_inbox(mcp, state, shutdown=shutdown))
        reaper = asyncio.create_task(_reap_loop(state, shutdown))
        heartbeat = asyncio.create_task(_heartbeat_loop(state, shutdown))
        watcher = asyncio.create_task(_wtmp_watcher_loop(state, shutdown))
        # Process any sentinels left from previously-killed servers.
        await _reap_sentinels(state)
        try:
            yield state
        finally:
            # Write logout FIRST — before stopping tasks or closing
            # anything.  The MCP subprocess may be killed at any moment
            # after Claude Code closes stdio, so the logout publish
            # must happen while the NATS connection is still healthy.
            if state.owns_relay:
                await _append_logout_event(state)
            await _shutdown_tasks(shutdown, [poller, reaper, heartbeat, watcher])
            if state.unread_path is not None:
                with suppress(FileNotFoundError):
                    state.unread_path.unlink()
            if state.owns_relay:
                try:
                    await state.relay.delete_session(state.session_key)
                except Exception:
                    logger.exception("Failed to delete session %s", state.session_key)
                await state.relay.close()

    mcp: FastMCP[ServerState] = FastMCP(
        "biff",
        instructions=(
            "Biff is a communication tool for software engineers. "
            "Use these tools to send messages, check presence, "
            "and coordinate with your team.\n\n"
            "All biff tool output is pre-formatted plain text using unicode "
            "characters for alignment. Always emit biff output verbatim — "
            "never reformat, never convert to markdown tables, never wrap "
            "in code fences or boxes."
        ),
        lifespan=lifespan,
    )

    register_all_tools(mcp, state)
    return mcp
