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
from biff.server.display_queue import DisplayItem


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

MAX_UNREAD_COUNT: int = 100
"""Maximum unread count written to the status file.

Corresponds to ``maxUnreadCount`` in the Z specification
(notification.tex §4).  Counts above this are clamped.
"""

# Captured eagerly during ``initialize`` (via SessionCaptureMiddleware)
# and refreshed on every tool call (belt path) so the background poller
# and NATS callbacks can send notifications outside a request context.
# See notification.tex CaptureSession operation.
_session: ServerSession | None = None


def capture_session(session: ServerSession) -> None:
    """Eagerly store the MCP session reference.

    Called from :class:`~biff.server.app._SessionCaptureMiddleware`
    during ``initialize`` so the suspenders notification path is
    available before any tool call or NATS subscription fires.

    The belt path in :func:`notify_tool_list_changed` continues to
    refresh the reference on every tool call, keeping it current
    if the client reconnects.
    """
    global _session
    _session = session


# Set by the ``tty`` tool so the unread file includes the session name.
_tty_name: str = ""

# Set by the ``mesg`` tool so the unread file includes availability state.
_biff_enabled: bool = True

# Set by the ``talk`` tool and background poller NATS subscription.
# ``_talk_partner`` drives subscription lifecycle in
# ``_manage_talk_subscription``; ``_talk_message`` feeds the talk
# tool description (always shows latest message for Claude).
_talk_partner: str | None = None
_talk_message: str = ""


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


def _reset_session() -> None:
    """Clear stored session, tty name, biff_enabled, talk — test isolation."""
    global _session, _tty_name, _biff_enabled
    global _talk_partner, _talk_message
    _session = None
    _tty_name = ""
    _biff_enabled = True
    _talk_partner = None
    _talk_message = ""


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


async def _sync_unread_file(
    state: ServerState,
    *,
    summary: UnreadSummary | None = None,
) -> None:
    """Write the unread status file with current display queue state.

    Serializes the full queue snapshot to ``display_items`` so the
    status bar can rotate through all items independently.  Called
    after any state change that might affect what the status bar shows.

    Pass *summary* to reuse an already-fetched :class:`UnreadSummary`
    and avoid a redundant relay call.
    """
    if state.unread_path is None:
        return
    if summary is None:
        summary = await state.relay.get_unread_summary(state.session_key)
    items = state.display_queue.snapshot()
    _write_unread_file(
        state.unread_path,
        summary,
        repo_name=state.config.repo_name,
        user=state.config.user,
        tty_name=_tty_name,
        biff_enabled=_biff_enabled,
        display_items=items,
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
    await _sync_unread_file(state, summary=summary)


async def refresh_wall(
    mcp: FastMCP[ServerState],
    state: ServerState,
    *,
    wall: WallPost | None | _Sentinel = _SENTINEL,
) -> None:
    """Update the ``wall`` tool description and display queue.

    When a wall is active, the description shows the current banner
    and a ``DisplayItem`` is added to the rotation queue with a unique
    source key (``wall:{posted_at}``).  Old walls stay in the queue
    until they expire naturally — the queue accumulates walls so the
    status bar can rotate through them.

    When no wall is active (cleared or all expired), all wall items
    are removed from the queue.

    Pass *wall* to skip the relay fetch when the caller already has
    the current wall (e.g. :func:`poll_inbox`).
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    from biff.server.tools.wall import (  # noqa: PLC0415
        WALL_BASE_DESCRIPTION,
        format_remaining,
    )

    tool = await mcp.get_tool("wall")
    if tool is None:
        return
    current = await state.relay.get_wall() if isinstance(wall, _Sentinel) else wall
    old_desc = tool.description
    queue = state.display_queue
    if current is None:
        tool.description = WALL_BASE_DESCRIPTION
        queue.remove_by_kind("wall")
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
        # Duration arithmetic: wall-clock difference → monotonic expiry.
        # Safe because we convert to a relative seconds delta, not an
        # absolute timestamp, before passing to the monotonic-based queue.
        seconds_remaining = (current.expires_at - datetime.now(UTC)).total_seconds()
        queue.add(
            DisplayItem(
                kind="wall",
                text=f"{sender}: {current.text}",
                source_key=f"wall:{current.posted_at.isoformat()}",
                expires_at=queue.expires_from_now(max(0.0, seconds_remaining)),
            )
        )
    if tool.description != old_desc:
        await notify_tool_list_changed()
    await _sync_unread_file(state)


TALK_BASE_DESCRIPTION = (
    "Start a real-time conversation with a teammate or agent. "
    "Incoming messages appear on the status bar automatically. "
    "Use /write to reply. Use talk_end to close."
)


async def refresh_talk(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Update the ``talk`` tool description with the latest talk message.

    Mirrors :func:`refresh_wall` — mutates the tool description so that
    ``notify_tool_list_changed()`` causes Claude Code to re-read the tool
    list, see a changed description, and trigger a UI re-render.  Without
    the description change, the notification is a no-op from Claude Code's
    perspective (it re-reads the same descriptions and does nothing).

    Queue management (add/remove talk items) is handled by callers:
    ``_on_talk_msg`` adds items, ``_manage_talk_subscription`` removes
    them on talk end.
    """
    tool = await mcp.get_tool("talk")
    if tool is None:
        return
    old_desc = tool.description
    if _talk_partner and _talk_message:
        tool.description = (
            f"[TALK] {_talk_message} — "
            f"Use /write @{_talk_partner} to reply. "
            "Use talk_end to close."
        )
    else:
        tool.description = TALK_BASE_DESCRIPTION
    if tool.description != old_desc:
        await notify_tool_list_changed()
    await _sync_unread_file(state)


async def _manage_talk_subscription(
    mcp: FastMCP[ServerState],
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

    # Unsubscribe old — and reset talk tool description
    if sub is not None:
        with suppress(Exception):
            await sub.unsubscribe()  # type: ignore[attr-defined]
        sub = None

    # Clear talk items whenever the partner changes (including talk end) —
    # stale messages from the previous partner must not rotate into view.
    state.display_queue.remove_by_kind("talk")

    if wanted is None or not isinstance(state.relay, NatsRelay):
        await refresh_talk(mcp, state)
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
                    display_text = f"@{sender}: {body}"
                    set_talk_message(display_text)
                    # Add to display queue — coalesce per sender so rapid
                    # messages replace the previous one instead of growing
                    # the queue without bound.
                    source_key = f"talk:{sender}"
                    state.display_queue.remove_by_source_key(source_key)
                    item = DisplayItem(
                        kind="talk",
                        text=display_text,
                        source_key=source_key,
                    )
                    state.display_queue.add(item)
                    state.display_queue.force_to_front(item.source_key)
                    await refresh_talk(mcp, state)
            except (json.JSONDecodeError, AttributeError, TypeError):
                pass

        sub = await nc.subscribe(  # pyright: ignore[reportUnknownMemberType]
            subject, cb=_on_talk_msg
        )
    except Exception:  # noqa: BLE001
        logger.debug("Failed to subscribe to talk notifications")
        sub = None

    return wanted, sub


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

    # Refresh talk tool description when talk message changes
    if _talk_message != last_talk:
        last_talk = _talk_message
        await refresh_talk(mcp, state)

    # Rotate display queue — talk items expire, wall items cycle
    if state.display_queue.advance_if_due():
        await _sync_unread_file(state, summary=summary)

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
                mcp, state, talk_partner_tracked, talk_sub
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
    display_items: list[DisplayItem] | None = None,
) -> None:
    """Write unread count to a JSON status file.

    Includes ``user``, ``repo``, ``tty_name``, ``biff_enabled``, and
    the full ``display_items`` list so the status bar can rotate
    through all items independently using time-based indexing.

    Failures are logged but never propagated — tool execution must not
    break because a status file could not be written.
    """
    items_list: list[dict[str, str]] = [
        {"kind": item.kind, "text": item.text} for item in (display_items or [])
    ]
    clamped_count = min(summary.count, MAX_UNREAD_COUNT)
    data: dict[str, object] = {
        "user": user,
        "repo": repo_name,
        "count": clamped_count,
        "tty_name": tty_name,
        "biff_enabled": biff_enabled,
        "display_items": items_list,
    }
    try:
        atomic_write(path, json.dumps(data, indent=2) + "\n")
    except OSError:
        logger.warning("Failed to write unread status file %s", path, exc_info=True)
