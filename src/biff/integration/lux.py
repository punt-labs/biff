"""Lux integration — session status dashboard applet.

Builds a lux element tree from session data (statusline JSON) and
unread state, using typed punt-lux element classes for compile-time
validation.  The background loop connects via ``LuxClient`` and
pushes updates every 5 seconds.

Follows the integration standard (L0-L3):
- L0: Sentinel file check via ``is_lux_enabled()``
- L2: Library import of ``punt_lux`` (guarded behind ImportError)
- Graceful degradation when punt-lux is absent
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from biff._stdlib import BIFF_DATA_DIR, is_lux_enabled
from biff.unread import SessionUnread, as_str_dict, read_session_unread

if TYPE_CHECKING:
    from punt_lux import LuxClient
    from punt_lux.protocol import Element

logger = logging.getLogger(__name__)

# ── Well-known paths ─────────────────────────────────────────────────

SESSION_DATA_DIR = BIFF_DATA_DIR / "session-data"
UNREAD_DIR = BIFF_DATA_DIR / "unread"

_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;]*[A-Za-z]|\][^\x07]*\x07?|[()][A-B012])")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

SCENE_ID = "biff-session-status"
FRAME_ID = "biff-session"
FRAME_TITLE = "Session Status"
FRAME_SIZE = (360, 280)


# ── Data loading ─────────────────────────────────────────────────────


def load_session_data(session_key: str) -> dict[str, object]:
    """Read ``~/.punt-labs/biff/session-data/{key}.json``, return ``{}`` on error."""
    path = SESSION_DATA_DIR / f"{session_key}.json"
    try:
        return as_str_dict(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _sanitize(text: str) -> str:
    """Strip ANSI escapes and control characters from user-supplied text."""
    text = _ANSI_RE.sub("", text)
    text = _CTRL_RE.sub("", text)
    return " ".join(text.split())


# ── Element builders (pure functions) ────────────────────────────────


def _git_text(session: dict[str, object]) -> str:
    """Extract repo name from session workspace data."""
    ws_raw = session.get("workspace")
    ws = as_str_dict(ws_raw)
    if ws:
        project_dir = ws.get("project_dir") or ws.get("current_dir", "")
    elif isinstance(ws_raw, str):
        project_dir = ws_raw
    else:
        return ""
    if not isinstance(project_dir, str) or not project_dir:
        return ""
    return Path(project_dir).name


def _context_fraction(session: dict[str, object]) -> float | None:
    """Extract context window usage as a 0.0-1.0 fraction."""
    cw = as_str_dict(session.get("context_window"))
    if not cw:
        return None
    pct = cw.get("used_percentage")
    if isinstance(pct, (int, float)):
        return pct / 100.0
    return None


def _active_session_count() -> int:
    """Count active session files in ``~/.punt-labs/biff/active/``."""
    active = BIFF_DATA_DIR / "active"
    try:
        return sum(1 for f in active.iterdir() if f.is_file())
    except OSError:
        return 0


def _cost_text(session: dict[str, object]) -> str:
    """Extract session cost as ``$X.XX``."""
    cost = as_str_dict(session.get("cost"))
    if not cost:
        return ""
    total = cost.get("total_cost_usd", 0)
    if isinstance(total, (int, float)) and total > 0:
        return f"${total:.2f}"
    return ""


def build_status_elements(
    session: dict[str, object],
    unread: SessionUnread | None,
) -> list[Element]:
    """Build lux element tree for the session status dashboard.

    The hero section is the biff identity and message state.
    Context bar and cost are secondary — cost is omitted when
    unavailable (Max plan users, no API key).
    """
    from punt_lux.protocol import (  # noqa: PLC0415
        ProgressElement,
        SeparatorElement,
        TextElement,
    )

    elements: list[Element] = []

    # ── Hero: biff identity and messaging ──
    if unread is None:
        elements.append(TextElement(id="identity", content="not configured"))
    else:
        name = unread.user or "biff"
        tty = f":{unread.tty_name}" if unread.tty_name else ""
        elements.append(TextElement(id="identity", content=f"{name}{tty}"))
        if not unread.biff_enabled:
            elements.append(TextElement(id="msg-status", content="messaging off"))
        else:
            label = "message" if unread.count == 1 else "messages"
            elements.append(
                TextElement(
                    id="msg-status",
                    content=f"{unread.count} {label}",
                )
            )

        # Presence count from active session files
        others = _active_session_count() - 1  # exclude self
        if others > 0:
            label = "other" if others == 1 else "others"
            elements.append(
                TextElement(id="presence", content=f"{others} {label} online")
            )

        # Wall/talk display items — sanitize user-supplied text
        for i, item in enumerate(unread.display_items):
            clean = _sanitize(item.text)
            if clean:
                prefix = "wall" if item.kind == "wall" else "talk"
                elements.append(
                    TextElement(
                        id=f"display-{i}",
                        content=f"[{prefix}] {clean}",
                    )
                )

    # ── Secondary: context and cost ──
    frac = _context_fraction(session)
    cost = _cost_text(session)
    if frac is not None or cost:
        elements.append(SeparatorElement())

    if frac is not None:
        elements.append(ProgressElement(id="ctx-bar", fraction=frac))

    if cost:
        elements.append(TextElement(id="cost", content=cost))

    return elements


# ── Rendering ────────────────────────────────────────────────────────


def render_session_status(client: LuxClient, session_key: str) -> None:
    """Load data, build elements, push to lux via ``show_async``."""
    session = load_session_data(session_key)
    unread_path = UNREAD_DIR / f"{session_key}.json"
    unread = read_session_unread(unread_path)
    elements = build_status_elements(session, unread)

    repo = _git_text(session)
    title = f"Biff: {repo}" if repo else "Biff"

    if elements:
        client.show_async(
            SCENE_ID,
            elements,
            frame_id=FRAME_ID,
            frame_title=title,
            frame_size=FRAME_SIZE,
        )


# ── Background loop ──────────────────────────────────────────────────


_FAILURE_ESCALATION_THRESHOLD = 3


def session_status_loop(
    client: LuxClient,
    session_key: str,
    stop_event: threading.Event,
    *,
    interval: float = 5.0,
) -> None:
    """Background thread: re-render dashboard every *interval* seconds."""
    consecutive_failures = 0
    while not stop_event.wait(interval):
        try:
            render_session_status(client, session_key)
            consecutive_failures = 0
        except Exception:  # noqa: BLE001
            consecutive_failures += 1
            if consecutive_failures >= _FAILURE_ESCALATION_THRESHOLD:
                logger.warning(
                    "Lux render failed %d times, stale",
                    consecutive_failures,
                    exc_info=True,
                )
            else:
                logger.debug("Lux render failed, retrying next tick", exc_info=True)


def start_session_applet(
    session_key: str,
    stop_event: threading.Event,
    *,
    interval: float = 5.0,
) -> threading.Thread | None:
    """Start the background dashboard thread if lux is enabled.

    Connects a ``LuxClient``, registers the Applications menu item,
    starts the background listener, and launches the render loop.
    Returns ``None`` if lux is not available (graceful degradation).
    """
    if not is_lux_enabled():
        return None

    try:
        from punt_lux import LuxClient as _LuxClient  # noqa: PLC0415
    except ImportError:
        logger.debug("punt-lux not installed, skipping lux applet")
        return None

    client = _LuxClient(name="biff")
    try:
        client.declare_menu_item({"id": "app-biff-session", "label": "Session Status"})
        client.connect()
        client.start_listener()
    except Exception:  # noqa: BLE001
        logger.debug("Failed to connect to lux display", exc_info=True)
        client.close()
        return None

    # Render once immediately, then start the periodic loop
    try:
        render_session_status(client, session_key)
    except Exception:  # noqa: BLE001
        logger.debug("Initial lux render failed", exc_info=True)

    def _loop_then_close() -> None:
        try:
            session_status_loop(client, session_key, stop_event, interval=interval)
        finally:
            client.close()

    thread = threading.Thread(
        target=_loop_then_close,
        daemon=True,
        name="biff-lux-applet",
    )
    thread.start()
    return thread
