"""Lux integration — session status dashboard applet.

Builds a lux element tree from session data (statusline JSON) and
unread state, using typed punt-lux element classes for compile-time
validation.  The background loop connects via ``LuxClient`` and
pushes updates every 5 seconds.

Follows the integration standard:
- L0: Socket connectivity check (try to connect, skip if display absent)
- L2: Library import of ``punt_lux`` (guarded behind ImportError)
- Graceful degradation when punt-lux is absent or display not running
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from biff._stdlib import BIFF_DATA_DIR, display_repo_name
from biff.unread import (
    SessionUnread,
    as_str_dict,
    read_session_unread,
)

if TYPE_CHECKING:
    from punt_lux import LuxClient
    from punt_lux.protocol import Element

logger = logging.getLogger(__name__)

# ── Well-known paths ─────────────────────────────────────────────────

SESSION_DATA_DIR = BIFF_DATA_DIR / "session-data"
UNREAD_DIR = BIFF_DATA_DIR / "unread"

_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;]*[A-Za-z]|\][^\x07]*\x07?|[()][A-B012])")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

FRAME_ID = "biff-session"
FRAME_SIZE = (180, 400)


def _scene_id(repo: str, tty: str) -> str:
    """Per-session scene ID within the shared biff frame."""
    base = f"biff-status-{repo}" if repo else "biff-status"
    return f"{base}-{tty}" if tty else base


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
        return max(0.0, min(1.0, pct / 100.0))
    return None


def _cost_text(session: dict[str, object]) -> str:
    """Extract session cost as ``$X.XX``."""
    cost = as_str_dict(session.get("cost"))
    if not cost:
        return ""
    total = cost.get("total_cost_usd", 0)
    if isinstance(total, (int, float)) and total > 0:
        return f"${total:.2f}"
    return ""


_PREVIEW_LEN = 20


def _truncate(text: str, length: int = _PREVIEW_LEN) -> str:
    """Truncate *text* to *length* chars with ellipsis when needed."""
    if len(text) <= length:
        return text
    return text[: length - 1] + "\u2026"


def _extract_display_messages(
    unread: SessionUnread,
) -> tuple[str, str]:
    """Extract wall and talk message bodies from display items.

    Strips the ``@user (ttyN): `` attribution prefix, returning
    just the message body for the first wall and first talk item.
    """
    wall_msg = ""
    talk_msg = ""
    for item in unread.display_items:
        clean = _sanitize(item.text)
        # Strip "@user (ttyN): " prefix — body follows the first ": "
        if ": " in clean:
            clean = clean.split(": ", maxsplit=1)[1]
        if item.kind == "wall" and clean and not wall_msg:
            wall_msg = clean
        elif item.kind == "talk" and clean and not talk_msg:
            talk_msg = clean
    return wall_msg, talk_msg


def _tree_section(
    element_id: str,
    preview: str,
    full_text: str,
) -> Element:
    """Build a tree node with a truncated preview and full-text child.

    Uses ``TreeElement`` (``imgui.tree_node``) for lightweight visual
    weight — just an arrow + text, no background fill.  ``flat=True``
    uses ``NoTreePushOnOpen`` so expanded children render at the same
    indent level as the parent.
    """
    from punt_lux.protocol import TreeElement  # noqa: PLC0415

    node: dict[str, object] = {"label": preview}
    if len(full_text) > _PREVIEW_LEN:
        node["children"] = [{"label": full_text}]
    return TreeElement(id=element_id, nodes=[node])


def build_status_elements(
    session: dict[str, object],
    unread: SessionUnread | None,
    *,
    prefix: str = "",
) -> list[Element]:
    """Build lux element tree for the session status dashboard.

    Pure function — all data is passed in, no I/O.  Each session card
    shows: repo, plan, messages+cost, wall, talk, context bar.

    Wall, talk, and plan use ``TreeElement`` (``imgui.tree_node``) for
    lightweight visual weight — just an arrow + text, no background
    fill.  Only shown when content exists.

    *prefix* namespaces element IDs so multiple sessions can coexist
    in the same frame without ID collisions.
    """
    from punt_lux.protocol import (  # noqa: PLC0415
        ProgressElement,
        TextElement,
    )

    def _id(name: str) -> str:
        return f"{prefix}{name}" if prefix else name

    elements: list[Element] = []

    # ── Messages + cost (single line) ──
    if unread is None:
        elements.append(TextElement(id=_id("msg-status"), content="not configured"))
    else:
        # ── Repo (identity comes first) ──
        if unread.repo:
            elements.append(
                TextElement(
                    id=_id("repo"),
                    content=display_repo_name(unread.repo),
                )
            )

        # ── Plan (tree node) ──
        if unread.plan:
            elements.append(
                _tree_section(_id("plan"), _truncate(unread.plan), unread.plan)
            )

        # ── Messages + cost ──
        if not unread.biff_enabled:
            msg_text = "messaging off"
        else:
            label = "message" if unread.count == 1 else "messages"
            msg_text = f"{unread.count} {label}"

        cost = _cost_text(session)
        if cost:
            msg_text = f"{msg_text}  {cost}"

        elements.append(TextElement(id=_id("msg-status"), content=msg_text))

        # ── Wall/talk (tree nodes) ──
        wall_msg, talk_msg = _extract_display_messages(unread)

        if wall_msg:
            elements.append(_tree_section(_id("wall"), _truncate(wall_msg), wall_msg))
        if talk_msg:
            elements.append(_tree_section(_id("talk"), _truncate(talk_msg), talk_msg))

    # ── Context bar (ends the section) ──
    frac = _context_fraction(session)
    if frac is not None:
        elements.append(ProgressElement(id=_id("ctx-bar"), fraction=frac))

    return elements


# ── Rendering ────────────────────────────────────────────────────────


def _element_ids(elements: list[Element]) -> tuple[str, ...]:
    """Return the ordered tuple of element IDs (structure fingerprint)."""
    return tuple(getattr(e, "id", None) or "" for e in elements)


def _element_patchable_value(element: Element) -> dict[str, object]:
    """Extract patchable fields from an element based on its kind."""
    kind = getattr(element, "kind", "")
    if kind == "text":
        return {"content": getattr(element, "content", "")}
    if kind == "progress":
        return {"fraction": getattr(element, "fraction", 0.0)}
    if kind == "tree":
        return {"nodes": getattr(element, "nodes", [])}
    return {}


def _load_and_build(
    session_key: str,
    tty: str,
) -> tuple[str, str, list[Element]]:
    """Load session data and build elements. Returns (repo, title, elements).

    *tty* is used to namespace element IDs (e.g. ``"tty1-"``).
    """
    session = load_session_data(session_key)
    unread_path = UNREAD_DIR / f"{session_key}.json"
    unread = read_session_unread(unread_path)
    id_prefix = f"{tty}-" if tty else ""
    elements = build_status_elements(session, unread, prefix=id_prefix)
    repo = _git_text(session)
    # Section header = identity (user:tty), not repo:tty
    if unread is not None:
        name = unread.user or "biff"
        tty_label = f":{unread.tty_name}" if unread.tty_name else ""
        title = f"{name}{tty_label}"
    else:
        title = f"biff:{tty}" if tty else "biff"
    return repo, title, elements


# ── Background loop ──────────────────────────────────────────────────


_FAILURE_ESCALATION_THRESHOLD = 3


def session_status_loop(
    client: LuxClient,
    session_key: str,
    tty: str,
    stop_event: threading.Event,
    *,
    interval: float = 5.0,
    initial_ids: tuple[str, ...] = (),
    initial_repo: str = "",
) -> None:
    """Background thread: re-render dashboard every *interval* seconds.

    Uses ``update_async`` (patches) when the element structure hasn't
    changed, preserving the window's z-order and collapsed state.
    Falls back to ``show_async`` when elements are added or removed.
    """
    from punt_lux.protocol import Patch  # noqa: PLC0415

    prev_ids = initial_ids
    prev_repo = initial_repo
    consecutive_failures = 0
    while not stop_event.wait(interval):
        try:
            repo, title, elements = _load_and_build(session_key, tty)
            if not elements or not repo:
                consecutive_failures = 0
                continue
            scene = _scene_id(repo, tty)
            cur_ids = _element_ids(elements)
            if cur_ids == prev_ids and repo == prev_repo:
                patches = [
                    Patch(id=eid, set=_element_patchable_value(el))
                    for eid, el in zip(cur_ids, elements, strict=True)
                    if eid
                ]
                if patches:
                    client.update_async(scene, patches)
            else:
                client.show_async(
                    scene,
                    elements,
                    title=title,
                    frame_id=FRAME_ID,
                    frame_title="Biff",
                    frame_size=FRAME_SIZE,
                    frame_layout="stack",
                )
            prev_ids = cur_ids
            prev_repo = repo
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
    tty: str = "",
    interval: float = 5.0,
) -> threading.Thread | None:
    """Start the background dashboard thread if lux is running.

    Connects a ``LuxClient``, registers the Applications menu item,
    starts the background listener, and launches the render loop.
    Returns ``None`` if lux is not available (graceful degradation).

    Availability is checked by attempting to connect to the display
    socket — no config file read needed.  If the display isn't
    running, ``connect()`` fails and we return ``None``.

    *tty* (e.g. ``"tty1"``) namespaces element IDs so multiple sessions
    can coexist in the shared biff frame without ID collisions.
    """
    # Skip config file check (is_lux_enabled) — the MCP server's repo_root
    # may be a parent workspace, making path resolution unreliable.
    # Instead, try connecting to the display socket directly.  If lux isn't
    # running, connect() fails and we bail.  Per lux agent recommendation.
    try:
        from punt_lux import LuxClient as _LuxClient  # noqa: PLC0415
    except ImportError:
        logger.debug("punt-lux not installed, skipping lux applet")
        return None

    client = _LuxClient(name="biff")
    try:
        client.declare_menu_item({"id": "app-biff-session", "label": "Session Status"})

        def _on_menu_click(_msg: object) -> None:
            """Re-render dashboard on Applications menu click."""
            logger.info("Menu click: rendering session status for %s", tty)
            try:
                repo, title, elements = _load_and_build(session_key, tty)
                logger.info("Menu click: repo=%s elements=%d", repo, len(elements))
                if elements and repo:
                    client.show_async(
                        _scene_id(repo, tty),
                        elements,
                        title=title,
                        frame_id=FRAME_ID,
                        frame_title="Biff",
                        frame_size=FRAME_SIZE,
                        frame_layout="stack",
                    )
                    logger.info("Menu click: show_async sent")
                else:
                    logger.warning(
                        "Menu click: no elements (repo=%s, elements=%d)",
                        repo,
                        len(elements),
                    )
            except Exception:  # noqa: BLE001
                logger.warning("Menu click render failed", exc_info=True)

        client.on_event("app-biff-session", "menu", _on_menu_click)
        client.connect()
        client.start_listener()
        logger.info(
            "Lux applet started: tty=%s connected=%s",
            tty,
            client.is_connected,
        )
    except Exception:  # noqa: BLE001
        logger.warning("Failed to connect to lux display", exc_info=True)
        client.close()
        return None

    # Render once immediately (full scene), then start the periodic loop.
    # Capture initial element IDs + repo so the loop can start with update_async.
    initial_ids: tuple[str, ...] = ()
    initial_repo = ""
    try:
        repo, title, elements = _load_and_build(session_key, tty)
        initial_repo = repo
        if elements and repo:
            client.show_async(
                _scene_id(repo, tty),
                elements,
                title=title,
                frame_id=FRAME_ID,
                frame_title="Biff",
                frame_size=FRAME_SIZE,
                frame_layout="stack",
            )
            initial_ids = _element_ids(elements)
    except Exception:  # noqa: BLE001
        logger.debug("Initial lux render failed", exc_info=True)

    def _loop_then_close() -> None:
        try:
            session_status_loop(
                client,
                session_key,
                tty,
                stop_event,
                interval=interval,
                initial_ids=initial_ids,
                initial_repo=initial_repo,
            )
        finally:
            client.close()

    thread = threading.Thread(
        target=_loop_then_close,
        daemon=True,
        name="biff-lux-applet",
    )
    thread.start()
    return thread
