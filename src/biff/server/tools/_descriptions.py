"""Dynamic tool description updates and inbox polling.

Refreshes tool descriptions based on current server state.
Called after every tool execution (belt) and by a background
poller (suspenders) so notifications stay fresh even between
tool calls.

After mutating the ``read_messages`` description, fires
``notifications/tools/list_changed`` so Claude Code re-reads
the tool list and sees the updated unread count.

Also writes an ``unread.json`` status file (when configured) so that
external tools like the Claude Code status bar can display a live
unread count without querying the MCP server.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from biff.models import UnreadSummary, WallPost
from biff.relay import atomic_write


class _Sentinel:
    """Sentinel for distinguishing 'not provided' from ``None``."""


_SENTINEL = _Sentinel()

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from mcp.server.session import ServerSession

    from biff.server.state import ServerState

logger = logging.getLogger(__name__)

_READ_MESSAGES_BASE = "Check your inbox for new messages. Marks all as read."

_DEFAULT_POLL_INTERVAL = 2.0
_DEFAULT_IDLE_THRESHOLD = 120.0  # 2 minutes — transition to napping
_DEFAULT_NAP_INTERVAL = 30.0  # 30 seconds — reduced polling while napping

# Updated on every tool call so the background poller can send
# notifications outside a request context.
_session: ServerSession | None = None

# Set by the ``tty`` tool so the unread file includes the session name.
_tty_name: str = ""

# Set by the ``mesg`` tool so the unread file includes availability state.
_biff_enabled: bool = True

# Set by the ``wall`` tool and background poller so the unread file
# includes the active wall text and sender for status bar display.
_wall_text: str = ""
_wall_from: str = ""

# Set by the ``talk`` tool and background poller NATS subscription so
# the unread file includes the active talk message for status bar display.
_talk_partner: str | None = None
_talk_message: str = ""

# Signalled by the NATS talk callback so a watcher task in app.py can
# fire notify_tool_list_changed() from its own asyncio task context.
# The NATS callback writes the unread file directly, but the notification
# must be sent from a task that shares the MCP session's write stream.
_talk_notify_event: asyncio.Event | None = None


def get_tty_name() -> str:
    """Return the module-level TTY name."""
    return _tty_name


def set_tty_name(name: str) -> None:
    """Update the module-level TTY name for unread file writes."""
    global _tty_name
    _tty_name = name


def set_biff_enabled(*, enabled: bool) -> None:
    """Update the module-level biff_enabled flag for unread file writes."""
    global _biff_enabled
    _biff_enabled = enabled


def get_talk_partner() -> str | None:
    """Return the active talk partner username, or ``None``."""
    return _talk_partner


def set_talk_partner(partner: str | None) -> None:
    """Update the active talk partner.  Clears talk message when ending."""
    global _talk_partner, _talk_message
    _talk_partner = partner
    if partner is None:
        _talk_message = ""


def set_talk_message(message: str) -> None:
    """Update the latest talk message for status bar display."""
    global _talk_message
    _talk_message = message


def get_talk_notify_event() -> asyncio.Event:
    """Return the talk notification event, creating it on first call.

    The event is lazily created so the asyncio event loop exists by the
    time it is instantiated.
    """
    global _talk_notify_event
    if _talk_notify_event is None:
        _talk_notify_event = asyncio.Event()
    return _talk_notify_event


def _reset_session() -> None:
    """Clear stored session, tty name, biff_enabled, wall, talk — test isolation."""
    global _session, _tty_name, _biff_enabled, _wall_text, _wall_from
    global _talk_partner, _talk_message, _talk_notify_event
    _session = None
    _tty_name = ""
    _biff_enabled = True
    _wall_text = ""
    _wall_from = ""
    _talk_partner = None
    _talk_message = ""
    _talk_notify_event = None


async def notify_tool_list_changed() -> None:
    """Fire ``notifications/tools/list_changed`` via the best available path.

    Belt path (inside a tool handler): queues the notification on the
    FastMCP Context so it piggybacks on the tool response.

    Suspenders path (background poller): sends directly on the stored
    ServerSession when no request context is active.
    """
    global _session

    # Belt path — inside a tool handler, Context is available.
    try:
        from fastmcp.server.dependencies import get_context  # noqa: PLC0415
        from mcp.types import ToolListChangedNotification  # noqa: PLC0415

        ctx = get_context()
        await ctx.send_notification(ToolListChangedNotification())
        # Always update — the client may have reconnected with a new session.
        _session = ctx.session
        return
    except RuntimeError:
        pass

    # Suspenders path — no request context, use stored session.
    # Bare Exception matches FastMCP's own _flush_notifications pattern —
    # notification delivery is best-effort and must never crash the poller.
    if _session is not None:
        try:
            await _session.send_tool_list_changed()
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to send tool list changed notification",
                exc_info=True,
            )


async def refresh_read_messages(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Update the ``read_messages`` tool description with unread count.

    When the user has unread messages, the description changes to show
    the count, e.g. ``"Check messages (2 unread). Marks all as read."``

    When the inbox is empty, the description reverts to the base text.

    After mutation, fires ``notifications/tools/list_changed`` so the
    client re-reads the tool list and sees the new description.

    If ``state.unread_path`` is set, also writes the unread summary to
    a JSON file for status bar consumption.
    """
    tool = await mcp.get_tool("read_messages")
    if tool is None:
        return
    summary = await state.relay.get_unread_summary(state.session_key)
    old_desc = tool.description
    if summary.count == 0:
        tool.description = _READ_MESSAGES_BASE
    else:
        tool.description = (
            f"Check messages ({summary.count} unread). Marks all as read."
        )
    if tool.description != old_desc:
        await notify_tool_list_changed()
    if state.unread_path is not None:
        _write_unread_file(
            state.unread_path,
            summary,
            repo_name=state.config.repo_name,
            user=state.config.user,
            tty_name=_tty_name,
            biff_enabled=_biff_enabled,
            wall_text=_wall_text,
            wall_from=_wall_from,
            talk_partner=_talk_partner,
            talk_message=_talk_message,
        )


async def refresh_wall(
    mcp: FastMCP[ServerState],
    state: ServerState,
    *,
    wall: WallPost | None | _Sentinel = _SENTINEL,
) -> None:
    """Update the ``wall`` tool description and module-level wall text.

    When a wall is active, the description shows the current banner.
    When no wall is active, the description reverts to base text.
    Also syncs ``_wall_text`` so the next unread file write includes it.

    Pass *wall* to skip the relay fetch when the caller already has
    the current wall (e.g. :func:`poll_inbox`).
    """
    global _wall_text, _wall_from

    from biff.server.tools.wall import (  # noqa: PLC0415
        WALL_BASE_DESCRIPTION,
        format_remaining,
    )

    tool = await mcp.get_tool("wall")
    if tool is None:
        return
    current = await state.relay.get_wall() if isinstance(wall, _Sentinel) else wall
    old_desc = tool.description
    if current is None:
        tool.description = WALL_BASE_DESCRIPTION
        _wall_text = ""
        _wall_from = ""
    else:
        remaining = format_remaining(current.expires_at)
        sender = f"@{current.from_user}"
        if current.from_tty:
            sender += f" ({current.from_tty})"
        tool.description = (
            f"[WALL] {current.text} — {sender}, "
            f"expires in {remaining}. "
            "Use wall(clear=True) to remove."
        )
        _wall_text = current.text
        _wall_from = current.from_user
        if current.from_tty:
            _wall_from += f" ({current.from_tty})"
    if tool.description != old_desc:
        await notify_tool_list_changed()
    # Re-write the unread file so wall text is synced to status bar
    if state.unread_path is not None:
        summary = await state.relay.get_unread_summary(state.session_key)
        _write_unread_file(
            state.unread_path,
            summary,
            repo_name=state.config.repo_name,
            user=state.config.user,
            tty_name=_tty_name,
            biff_enabled=_biff_enabled,
            wall_text=_wall_text,
            wall_from=_wall_from,
            talk_partner=_talk_partner,
            talk_message=_talk_message,
        )


async def _manage_talk_subscription(
    state: ServerState,
    current_partner: str | None,
    sub: object | None,
) -> tuple[str | None, object | None]:
    """Subscribe or unsubscribe to talk notifications as partner changes.

    Returns ``(new_partner, new_sub)`` for the caller to track.
    When the talk partner changes, the old subscription is dropped and
    a new one created.  NATS-only — no-ops for non-NATS relays.
    """
    from biff.nats_relay import NatsRelay  # noqa: PLC0415

    wanted = _talk_partner
    if wanted == current_partner:
        return current_partner, sub

    # Unsubscribe old
    if sub is not None:
        with suppress(Exception):
            await sub.unsubscribe()  # type: ignore[attr-defined]
        sub = None

    if wanted is None or not isinstance(state.relay, NatsRelay):
        return wanted, None

    # Subscribe new — capture messages from talk partner on status line
    try:
        nc = await state.relay.get_nc()
        subject = state.relay.talk_notify_subject(state.config.user)

        async def _on_talk_msg(msg: object) -> None:
            try:
                data = json.loads(msg.data)  # type: ignore[attr-defined]
                sender = data.get("from", "")
                body = data.get("body", "")
                from_key = data.get("from_key", "")

                # Reject self-echo: if the notification came from
                # this session, ignore it (same user, different tty).
                if from_key and from_key == state.session_key:
                    return

                # _talk_partner may be "user:tty" but notification
                # from field is always just the username.
                partner_user = (
                    _talk_partner.split(":")[0]
                    if _talk_partner and ":" in _talk_partner
                    else _talk_partner
                )
                if sender and sender == partner_user and body:
                    set_talk_message(f"@{sender}: {body}")
                    await _sync_talk_to_file(state)
                    # Signal the watcher task in app.py to fire
                    # notify_tool_list_changed() from its own context.
                    get_talk_notify_event().set()
            except (json.JSONDecodeError, AttributeError, TypeError):
                pass

        sub = await nc.subscribe(  # pyright: ignore[reportUnknownMemberType]
            subject, cb=_on_talk_msg
        )
    except Exception:  # noqa: BLE001
        logger.debug("Failed to subscribe to talk notifications")
        sub = None

    return wanted, sub


async def _sync_talk_to_file(state: ServerState) -> None:
    """Rewrite the unread file with current talk state.

    Called when the NATS subscription callback updates ``_talk_message``
    between poller ticks.
    """
    if state.unread_path is None:
        return
    summary = await state.relay.get_unread_summary(state.session_key)
    _write_unread_file(
        state.unread_path,
        summary,
        repo_name=state.config.repo_name,
        user=state.config.user,
        tty_name=_tty_name,
        biff_enabled=_biff_enabled,
        wall_text=_wall_text,
        wall_from=_wall_from,
        talk_partner=_talk_partner,
        talk_message=_talk_message,
    )


async def _active_tick(
    mcp: FastMCP[ServerState],
    state: ServerState,
    last_count: int,
    last_wall: tuple[str, str],
    last_talk: str,
) -> tuple[int, tuple[str, str], str]:
    """One active-mode poller tick: check inbox, wall, and talk changes.

    Returns updated ``(count, wall_key, talk_message)`` tracking state.
    """
    summary = await state.relay.get_unread_summary(state.session_key)
    if summary.count != last_count:
        last_count = summary.count
        await refresh_read_messages(mcp, state)

    # Check wall — key on (text, posted_at) so re-posts trigger refresh.
    current_wall = await state.relay.get_wall()
    wall_key = (
        (current_wall.text, current_wall.posted_at.isoformat())
        if current_wall
        else ("", "")
    )
    if wall_key != last_wall:
        last_wall = wall_key
        await refresh_wall(mcp, state, wall=current_wall)

    # Rewrite unread file when talk message changes (NATS callback updates it)
    if _talk_message != last_talk:
        last_talk = _talk_message
        await _sync_talk_to_file(state)

    return last_count, last_wall, last_talk


async def poll_inbox(
    mcp: FastMCP[ServerState],
    state: ServerState,
    *,
    shutdown: asyncio.Event | None = None,
    interval: float = _DEFAULT_POLL_INTERVAL,
    idle_threshold: float = _DEFAULT_IDLE_THRESHOLD,
    nap_interval: float = _DEFAULT_NAP_INTERVAL,
) -> None:
    """Background task: poll inbox and wall, refresh notifications on change.

    Runs for the lifetime of the MCP server.  In **active** mode,
    polls the relay every *interval* seconds (normal operation).
    After *idle_threshold* seconds with no tool call, transitions to
    **napping**: reduces polling to *nap_interval* but keeps the NATS
    connection alive so KV watches continue to deliver wall/session
    changes in real-time.

    The poller always ticks at *interval* (2s default).  During
    napping, most ticks are cheap no-ops (datetime comparison).
    This keeps wake-up responsive — at most 2s lag when ``touch()``
    clears napping.

    When *shutdown* is set, exits cleanly between iterations —
    no NATS operations are interrupted mid-flight.

    Also manages a NATS subscription for talk notifications: when a
    talk partner is active, incoming messages are captured and written
    to the unread status file for status bar display.
    """
    tracker = state.activity
    last_count = -1  # Force initial refresh
    last_wall: tuple[str, str] = ("", "")  # Force initial refresh
    last_talk = ""
    talk_partner_tracked: str | None = None
    talk_sub: object | None = None

    try:
        while shutdown is None or not shutdown.is_set():
            if shutdown is not None:
                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=interval)
                    return  # Shutdown requested
                except TimeoutError:
                    pass
            else:
                await asyncio.sleep(interval)

            # Manage talk subscription lifecycle
            talk_partner_tracked, talk_sub = await _manage_talk_subscription(
                state, talk_partner_tracked, talk_sub
            )

            # Transition: active → napping (connection stays open)
            if not tracker.napping and tracker.idle_seconds() > idle_threshold:
                tracker.enter_nap()

            # Napping: reduced-frequency polling (KV watcher is primary for wall)
            if tracker.napping:
                if tracker.seconds_since_nap_poll() < nap_interval:
                    continue
                last_count, last_wall, last_talk = await _active_tick(
                    mcp, state, last_count, last_wall, last_talk
                )
                tracker.record_nap_poll()
                continue

            last_count, last_wall, last_talk = await _active_tick(
                mcp, state, last_count, last_wall, last_talk
            )
    finally:
        if talk_sub is not None:
            with suppress(Exception):
                await talk_sub.unsubscribe()  # type: ignore[attr-defined]


def _write_unread_file(
    path: Path,
    summary: UnreadSummary,
    *,
    repo_name: str,
    user: str,
    tty_name: str,
    biff_enabled: bool,
    wall_text: str = "",
    wall_from: str = "",
    talk_partner: str | None = None,
    talk_message: str = "",
) -> None:
    """Write unread count to a JSON status file.

    Includes ``user``, ``repo``, ``tty_name``, ``biff_enabled``,
    ``wall``, ``wall_from``, and talk state so the status line can
    display identity, session, availability, wall banner, and active
    talk messages.

    Failures are logged but never propagated — tool execution must not
    break because a status file could not be written.
    """
    data: dict[str, object] = {
        "user": user,
        "repo": repo_name,
        "count": summary.count,
        "tty_name": tty_name,
        "biff_enabled": biff_enabled,
        "wall": wall_text,
        "wall_from": wall_from,
    }
    if talk_partner is not None:
        data["talk_partner"] = talk_partner
        data["talk_message"] = talk_message
    try:
        atomic_write(path, json.dumps(data, indent=2) + "\n")
    except OSError:
        logger.warning("Failed to write unread status file %s", path, exc_info=True)
