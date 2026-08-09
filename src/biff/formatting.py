"""Domain-level formatting functions for biff output.

Shared by both MCP tool modules and CLI commands. These compose
the primitive table/column layer in ``biff._formatting``
into tool-specific output: who tables, finger blocks, last history,
wall banners, and read message tables.

The primitive layer (``ColumnSpec``, ``format_table``, ``format_idle``,
``last_component``) lives in ``biff._formatting``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from biff._formatting import (
    HEADER_PREFIX,
    ROW_PREFIX,
    TABLE_WIDTH,
    ColumnSpec,
    clip_to_width,
    fmt_cell,
    format_idle,
    format_table,
    last_component,
    terminal_safe,
    visible_width,
    wrap_cells,
)
from biff._stdlib import display_repo_name
from biff.models import Message, SessionEvent, UserSession, WallPost

__all__ = [
    "HEADER_PREFIX",
    "LAST_SPECS",
    "READ_SPECS",
    "WHO_SPECS",
    "ColumnSpec",
    "format_finger",
    "format_finger_multi",
    "format_idle",
    "format_last",
    "format_read",
    "format_read_dual",
    "format_remaining",
    "format_table",
    "format_talk_echo",
    "format_talk_end",
    "format_talk_line",
    "format_tty_block",
    "format_user_header",
    "format_wall",
    "format_wall_confirmation",
    "format_wall_status_line",
    "format_who",
    "last_component",
    "pair_events",
    "parse_duration",
    "sanitize_wall_message",
    "sanitized_address",
    "sanitized_sender",
    "terminal_safe",
    "visible_text",
]

# Never wrap a talk body narrower than this, however long the sender
# label + timestamp lead grows.
_TALK_WRAP_MIN = 24

# Cap the rendered sender label so a forged, boundary-slipped label can never
# drive the per-line wrap indent (defense in depth for the O(label x body)
# amplification — see TalkNotification.from_payload, biff-7g7).
_MAX_LABEL_WIDTH = 40

# clip_to_width (via _truncate) only clips when visible_width(text) exceeds
# the cell-width budget — a label built entirely of zero-cell code points
# (combining marks, variation selectors) has visible_width == 0 no matter how
# many characters it holds, so the cell-width axis alone never triggers a
# clip. Bounding raw character count closes that gap independently of which
# axis — cells or code points — a forged payload tries to exploit. Sized a
# few times over _MAX_LABEL_WIDTH so legitimate wide-glyph content (CJK,
# emoji, which need one code point per cell) is never cut before the
# cell-width clip gets a chance to run.
_MAX_LABEL_CHARS = _MAX_LABEL_WIDTH * 4

# format_user_header's two-column "Login: ... Name: ..." layout — fmt_cell
# pads the left column to this width but never truncates, so the left
# column's content must be pre-clipped or an unbounded field renders one
# unbounded line instead of a fixed-width column.
_LOGIN_COL_WIDTH = 38


def _truncate(text: str, width: int) -> str:
    """Return *text* clipped to *width* terminal cells, ending with an ellipsis.

    Delegates to :func:`biff._formatting.clip_to_width` so labels clip on the
    same wcwidth cell-width basis as the table primitives and
    :func:`format_talk_line`'s wrap — a wide glyph (CJK, emoji) at the clip
    boundary is never undercounted the way code-point ``len()`` would
    undercount it.
    """
    return clip_to_width(text, width)


def _sanitized_label(from_user: str, from_tty: str, tty_format: str) -> str:
    """Neutralise, compose, and clip a sender/identity label as one unit.

    Shared by :func:`sanitized_sender` (``"user (tty)"``, for headers) and
    :func:`sanitized_address` (``"user:tty"``, for copy-pasteable reply
    addresses) — both compose two wire fields with no ``max_length`` and
    both must clip the WHOLE composed string, not just *from_user*, or the
    cap is defeated by concatenating an unbounded *from_tty* after it.
    *tty_format* is a ``str.format``-style template taking the sanitised
    user then the sanitised tty, e.g. ``"{} ({})"`` or ``"{}:{}"``.

    Bounded on two independent axes: :data:`_MAX_LABEL_CHARS` caps raw
    character count before :data:`_MAX_LABEL_WIDTH` caps terminal cell
    width, so a label made entirely of zero-cell code points (combining
    marks, variation selectors) cannot sail through unbounded just because
    it is zero-width by construction — see :func:`clip_to_width`.
    """
    label = terminal_safe(from_user)
    if from_tty:
        label = tty_format.format(label, terminal_safe(from_tty))
    return _truncate(label[:_MAX_LABEL_CHARS], _MAX_LABEL_WIDTH)


def sanitized_sender(from_user: str, from_tty: str = "") -> str:
    """Return a wall/talk sender label safe to fold into a wrap-width budget.

    Neutralises *from_user* — and *from_tty*, when given — via
    :func:`terminal_safe`, composes ``"user (tty)"`` when a tty is present,
    then clips the WHOLE composed string to :data:`_MAX_LABEL_WIDTH`.

    Neither ``WallPost.from_user`` nor ``WallPost.from_tty`` carries a
    ``max_length`` on the wire. Clipping only the ``from_user`` piece and
    then concatenating an unbounded ``from_tty`` after it defeats the cap —
    the combined label is exactly as unboundable as an uncapped
    ``from_user`` alone, and collapses a caller's wrap-width budget toward 1
    the same way (the O(label x body) amplification
    :func:`format_talk_line` already guards against). Every render site
    that folds a sender into a header line or a wrap-width budget must clip
    through this one function rather than reimplement the two-step
    transform.
    """
    return _sanitized_label(from_user, from_tty, "{} ({})")


def sanitized_address(from_user: str, from_tty: str = "") -> str:
    """Return a copy-pasteable ``user:tty`` address, bounded like ``sanitized_sender``.

    ``who``/``last``/``read`` render a ``user:tty`` reply address — for
    pasting straight into ``/write`` — rather than :func:`sanitized_sender`'s
    ``user (tty)`` header form, but the underlying hazard is identical:
    ``UserSession.user``/``tty_name``, ``SessionEvent.user``/``tty_name``,
    and ``Message.from_user``/``from_tty`` all lack a ``max_length`` on the
    wire. Every table these addresses render into (``WHO_SPECS``,
    ``LAST_SPECS``, ``READ_SPECS``) either widens a *fixed* column for
    every row from the longest cell in the table
    (:func:`~biff._formatting.format_table`'s fixed-column sizing), or — for
    ``READ_SPECS``'s variable ``MESSAGE`` column — collapses the shared wrap
    budget toward its floor, multiplying the damage across every row in the
    table rather than just the forged one. Clipping the whole composed
    address bounds both failure modes the same way
    :func:`sanitized_sender` already does for wall/talk headers.
    """
    return _sanitized_label(from_user, from_tty, "{}:{}")


# Wrap budget for free-form text rendered under the standard ROW_PREFIX
# indent (wall body, the finger Host:/Dir: line).
_ROW_TEXT_WIDTH = TABLE_WIDTH - len(ROW_PREFIX)

# Two distinct fallbacks for two distinct failure modes. Both cover a body
# that is non-empty on the wire but renders as nothing visible — a
# confirmation or read-back must never silently swallow the sender's
# entire message and look like it succeeded, or like nothing arrived
# (biff-2sw round 4, round 6).
#
# terminal_safe actually removed characters (control/escape bytes) and
# nothing printable survived:
_NO_PRINTABLE_TEXT = "(message contained no printable text)"
# terminal_safe changed nothing — the text was whitespace-only to begin
# with. Saying "no printable text" here would be false: whitespace *is*
# printable (str.isprintable() is True for spaces), nothing was stripped,
# the text just has nothing visible to show.
_NO_VISIBLE_CONTENT = "(message had no visible content)"

# The finger "Plan:" body hangs one level deeper than ROW_PREFIX.
_PLAN_INDENT = "    "
_PLAN_TEXT_WIDTH = TABLE_WIDTH - len(_PLAN_INDENT)


def visible_text(raw: str) -> str:
    """Return *raw* neutralised by :func:`terminal_safe`, or an honest fallback.

    Every render site that echoes remote-controlled text back to a human —
    a wall banner, a talk confirmation, a plan read-back — shares the same
    question: does this text have a glyph worth showing, and if not, why
    not?  There are two distinct reasons, and conflating them produces a
    false claim:

    * :func:`terminal_safe` left something behind with nonzero terminal
      width once surrounding whitespace is discounted
      (:func:`visible_width` of the stripped text > 0) — there is content,
      report it as-is.  Otherwise there is nothing visible, and the
      reason splits in two:

      * :func:`terminal_safe` left *something* behind, but it renders no
        glyph — e.g. *raw* was whitespace-only to begin with (spaces
        survive filtering; a blank cell isn't a shown glyph), or *raw*
        was combining marks / variation selectors with no base character
        to attach to.  These are ``str.isprintable()`` and not
        whitespace, so they survive both filtering and ``.strip()``, but
        carry zero terminal width — :func:`visible_width` catches them
        where ``.strip()`` truthiness would not.  Reported as
        :data:`_NO_VISIBLE_CONTENT`.
      * :func:`terminal_safe` left nothing behind at all — *raw* was
        empty, or every character in it was control/escape and got
        stripped.  Reported as :data:`_NO_PRINTABLE_TEXT`.

    :func:`visible_width` — not ``str.strip()`` truthiness alone — is the
    discriminator, because it is the same terminal-cell metric this
    module already trusts for wrap and clip decisions; a string can be
    non-empty and non-whitespace under ``.strip()`` while still occupying
    zero cells (combining marks, variation selectors).

    Callers that treat an empty *raw* as a deliberate, meaningful choice
    (e.g. ``/plan ""`` clearing a plan) must check that case themselves
    before calling this — an empty string here is reported the same as
    any other invisible text, not passed through blank.
    """
    safe = terminal_safe(raw)
    if visible_width(safe.strip()) > 0:
        return safe
    if not safe:
        return _NO_PRINTABLE_TEXT
    return _NO_VISIBLE_CONTENT


# ---------------------------------------------------------------------------
# Duration parsing (shared by wall CLI + MCP tool)
# ---------------------------------------------------------------------------

_DURATION_UNITS: dict[str, int] = {"m": 60, "h": 3600, "d": 86400}
_MAX_DURATION = timedelta(days=3)  # Capped by sessions KV bucket TTL
_DEFAULT_DURATION = timedelta(hours=1)


def parse_duration(s: str) -> timedelta:
    """Parse a human duration string like ``30m``, ``2h``, ``1d``.

    Returns :data:`_DEFAULT_DURATION` (1 hour) when *s* is empty.
    """
    s = s.strip().lower()
    if not s:
        return _DEFAULT_DURATION
    if len(s) < 2 or s[-1] not in _DURATION_UNITS:
        msg = f"Unrecognized duration {s!r}. Use 30m, 2h, 1d, 3d."
        raise ValueError(msg)
    try:
        n = int(s[:-1])
    except ValueError:
        msg = f"Unrecognized duration {s!r}. Use 30m, 2h, 1d, 3d."
        raise ValueError(msg) from None
    if n <= 0:
        msg = "Duration must be positive."
        raise ValueError(msg)
    try:
        td = timedelta(seconds=n * _DURATION_UNITS[s[-1]])
    except OverflowError:
        msg = f"Duration {s!r} exceeds maximum of 3 days."
        raise ValueError(msg) from None
    if td > _MAX_DURATION:
        msg = f"Duration {s!r} exceeds maximum of 3 days."
        raise ValueError(msg)
    return td


def format_remaining(expires_at: datetime) -> str:
    """Human-readable time remaining until *expires_at*."""
    remaining = expires_at - datetime.now(UTC)
    total_seconds = int(remaining.total_seconds())
    if total_seconds <= 0:
        return "expired"
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours > 0:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m" if minutes else "<1m"


# ---------------------------------------------------------------------------
# /who — session list
# ---------------------------------------------------------------------------

WHO_SPECS: list[ColumnSpec] = [
    ColumnSpec("NAME", min_width=10),
    ColumnSpec("K", min_width=1),
    ColumnSpec("REPO", min_width=4),
    ColumnSpec("IDLE", min_width=4),
    ColumnSpec("S", min_width=1),
    ColumnSpec("P", min_width=1),
    ColumnSpec("HOST", min_width=4),
]


def _format_who_name(s: UserSession) -> str:
    """Render a session as a copy-pasteable address for ``/write``.

    Routed through :func:`sanitized_address` — ``UserSession.user`` and
    ``tty_name`` carry no ``max_length`` on the wire, and NAME is a
    *fixed* :data:`WHO_SPECS` column, so an unbounded value here would
    widen every row in the table, not just the forged session's own row.
    """
    tty = terminal_safe(s.tty_name) or (s.tty[:8] if s.tty else "")
    return sanitized_address(s.user, tty)


def _format_who_kind(s: UserSession) -> str:
    """Render a kind tag for the ``/who`` table. Empty for humans."""
    if s.kind == "agent":
        return "[A]"
    return ""


def format_who(sessions: list[UserSession]) -> str:
    """Build a columnar table matching BSD ``w(1)`` style.

    The NAME column renders ``user:tty`` so agents can copy the
    address directly into ``/write``.  P column shows ``+`` if the
    session has a plan, ``-`` otherwise.  Use ``/finger user`` to
    see the full plan text.

    ``UserSession.hostname`` has no ``max_length`` on the wire either, and
    HOST is a *fixed* column like NAME — clipped via ``_truncate`` for the
    same reason :func:`_format_who_name` routes through
    :func:`sanitized_address`.
    """
    rows: list[list[str]] = [
        [
            _format_who_name(s),
            _format_who_kind(s),
            display_repo_name(s.repo) or "-",
            format_idle(s.last_active),
            "+" if s.biff_enabled else "-",
            "+" if s.plan else "-",
            _truncate(terminal_safe(s.hostname), _MAX_LABEL_WIDTH) or "-",
        ]
        for s in sessions
    ]
    return format_table(WHO_SPECS, rows)


# ---------------------------------------------------------------------------
# /finger — user status query
# ---------------------------------------------------------------------------


def _format_finger_idle(dt: datetime) -> str:
    """Format idle time matching BSD ``finger(1)`` style.

    Examples: ``0:03``, ``3:45``, ``1 day 7:22``
    """
    now = datetime.now(UTC)
    total_seconds = max(0, int((now - dt).total_seconds()))
    minutes = total_seconds // 60
    hours = minutes // 60
    days = hours // 24

    if days > 0:
        return f"{days} day{'s' if days > 1 else ''} {hours % 24}:{minutes % 60:02d}"
    return f"{hours}:{minutes % 60:02d}"


def format_user_header(session: UserSession) -> str:
    """Format the user-level header (shown once per user).

    ``UserSession.user``/``kind``/``display_name`` have no ``max_length``
    on the wire, and :func:`fmt_cell` pads its column to width but never
    truncates — an unbounded field would render one unbounded line instead
    of the intended fixed-width two-column layout. Each composed piece is
    clipped before padding.
    """
    user = terminal_safe(session.user)
    login_label = user
    if session.kind:
        login_label = f"{user} [{terminal_safe(session.kind)}]"
    left = _truncate(f"Login: {login_label}", _LOGIN_COL_WIDTH)
    mesg = "on" if session.biff_enabled else "off"
    if session.display_name:
        name = _truncate(terminal_safe(session.display_name), _MAX_LABEL_WIDTH)
        right = f"Name: {name}"
        line1 = f"▶  {fmt_cell(left, _LOGIN_COL_WIDTH, 'left')}{right}"
        line2 = f"   Messages: {mesg}"
        return f"{line1}\n{line2}"
    right = f"Messages: {mesg}"
    return f"▶  {fmt_cell(left, _LOGIN_COL_WIDTH, 'left')}{right}"


def format_tty_block(session: UserSession) -> str:
    """Format per-TTY details (on-since, host/dir, plan)."""
    idle = _format_finger_idle(session.last_active)
    since = session.last_active.strftime("%a %b %d %H:%M (%Z)")
    tty_label = terminal_safe(session.tty_name) or (
        session.tty[:8] if session.tty else "?"
    )

    lines = [f"   On since {since} on {tty_label}, idle {idle}"]
    if session.hostname or session.pwd:
        host = terminal_safe(session.hostname) or "?"
        pwd = terminal_safe(session.pwd) or "?"
        host_line = f"Host: {host}  Dir: {pwd}"
        chunks = wrap_cells(host_line, _ROW_TEXT_WIDTH, preserve_whitespace=True)
        lines.extend(ROW_PREFIX + chunk for chunk in chunks)
    plan = terminal_safe(session.plan)
    if plan:
        lines.append("   Plan:")
        chunks = wrap_cells(plan, _PLAN_TEXT_WIDTH, preserve_whitespace=True)
        lines.extend(_PLAN_INDENT + chunk for chunk in chunks)
    else:
        lines.append("   No Plan.")
    return "\n".join(lines)


def format_finger(session: UserSession) -> str:
    """Format a single session in BSD ``finger(1)`` style."""
    return f"{format_user_header(session)}\n{format_tty_block(session)}"


def format_finger_multi(sessions: list[UserSession]) -> str:
    """Format all sessions for a user (header once, multiple tty blocks)."""
    by_idle = sorted(sessions, key=lambda s: s.last_active, reverse=True)
    header = format_user_header(by_idle[0])
    tty_blocks = [format_tty_block(s) for s in by_idle]
    return header + "\n" + "\n".join(tty_blocks)


# ---------------------------------------------------------------------------
# /last — session history
# ---------------------------------------------------------------------------

LAST_SPECS: list[ColumnSpec] = [
    ColumnSpec("NAME", min_width=10),
    ColumnSpec("REPO", min_width=4),
    ColumnSpec("HOST", min_width=4),
    ColumnSpec("LOGIN", min_width=16),
    ColumnSpec("LOGOUT", min_width=15),
    ColumnSpec("DURATION", min_width=8, fixed=False),
]


def _format_duration(login_ts: datetime, logout_ts: datetime) -> str:
    """Format duration between login and logout as ``H:MM``."""
    total_seconds = max(0, int((logout_ts - login_ts).total_seconds()))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    return f"{hours}:{minutes:02d}"


def _format_timestamp(dt: datetime) -> str:
    """Format a datetime as ``Mon Feb 22 14:01``."""
    return dt.strftime("%a %b %d %H:%M")


def pair_events(
    events: list[SessionEvent],
) -> list[tuple[SessionEvent, SessionEvent | None]]:
    """Pair login events with their corresponding logout events.

    Returns a list of ``(login, logout | None)`` tuples in the same
    relative order as their login events appear in *events*.  A
    ``None`` logout means the session is still active or no logout
    was recorded — the caller uses ``active_keys`` to distinguish
    between the two.
    """
    logouts: dict[str, list[SessionEvent]] = {}
    logins: list[SessionEvent] = []

    for event in events:
        if event.event == "logout":
            logouts.setdefault(event.session_key, []).append(event)
        else:
            logins.append(event)

    pairs: list[tuple[SessionEvent, SessionEvent | None]] = []
    for login in logins:
        key = login.session_key
        matching_logout: SessionEvent | None = None
        if key in logouts:
            for i, lo in enumerate(logouts[key]):
                if lo.timestamp >= login.timestamp:
                    matching_logout = lo
                    logouts[key].pop(i)
                    break
        pairs.append((login, matching_logout))

    return pairs


def format_last(
    pairs: list[tuple[SessionEvent, SessionEvent | None]],
    active_keys: set[str],
) -> str:
    """Build a columnar table matching Unix ``last(1)`` style.

    ``SessionEvent.user``/``tty_name``/``hostname`` have no ``max_length``
    on the wire, and NAME/HOST are *fixed* :data:`LAST_SPECS` columns —
    bounded the same way :func:`_format_who_name`/:func:`format_who` bound
    the equivalent ``/who`` fields.
    """
    rows: list[list[str]] = []
    for login, logout in pairs:
        tty = terminal_safe(login.tty_name) or (login.tty[:8] if login.tty else "")
        name = sanitized_address(login.user, tty)
        repo = display_repo_name(login.repo) or "-"
        host = _truncate(terminal_safe(login.hostname), _MAX_LABEL_WIDTH) or "-"
        login_str = _format_timestamp(login.timestamp)
        if logout is not None:
            logout_str = _format_timestamp(logout.timestamp)
            duration = _format_duration(login.timestamp, logout.timestamp)
        elif login.session_key in active_keys:
            logout_str = "still logged in"
            duration = "-"
        else:
            logout_str = "gone"
            duration = "-"
        rows.append([name, repo, host, login_str, logout_str, duration])

    if not rows:
        return "No session history."

    return format_table(LAST_SPECS, rows)


# ---------------------------------------------------------------------------
# /wall — team broadcast
# ---------------------------------------------------------------------------


def sanitize_wall_message(message: str) -> str:
    """Normalize a wall message for posting.

    Strips all non-printable characters via :func:`terminal_safe` (control,
    ESC/OSC introducers, and other C0/C1 chars) and collapses runs of
    whitespace to single spaces.  This is input-side hygiene; render sites
    sanitize again at the output boundary (biff-lbj).
    """
    return " ".join(terminal_safe(message).split())


def format_wall(wall: WallPost) -> str:
    """Format a wall post for display, wrapped to the table width.

    Wall text can run up to 512 characters (biff-2sw) — a CJK- or
    emoji-heavy post would otherwise overflow the 80-column terminal on a
    single unwrapped line, the same defect class fixed for who/read/talk.

    ``WallPost.text`` requires at least one character on the wire, but a
    body made entirely of control/escape characters renders to nothing once
    :func:`terminal_safe` strips them — falling through would show teammates
    a bare, empty-looking ``▶  WALL`` banner with no hint the poster's
    message was silently dropped.  See :func:`visible_text` for how a
    control-only body is distinguished from a whitespace-only one.

    Neither ``WallPost.from_user`` nor ``WallPost.from_tty`` has a
    ``max_length``, so the composed sender is capped via
    :func:`sanitized_sender` the same way :func:`format_talk_line` caps its
    label — otherwise a forged sender renders one unbounded header line.
    """
    remaining = format_remaining(wall.expires_at)
    sender = sanitized_sender(wall.from_user, wall.from_tty)
    text = visible_text(wall.text)
    chunks = wrap_cells(text, _ROW_TEXT_WIDTH, preserve_whitespace=True) or [""]
    body = "\n".join(ROW_PREFIX + chunk for chunk in chunks)
    return f"▶  WALL from {sender} ({remaining} remaining)\n{body}"


def format_wall_confirmation(post: WallPost) -> str:
    """Format the post-confirmation shown to the poster, wrapped to table width.

    Echoes the same up-to-512-char, potentially CJK/emoji-heavy text
    :func:`format_wall` renders on read-back, so the confirmation shown at
    post time must wrap the same way instead of embedding raw text on one
    unbounded line (biff-2sw). A control-only body — see :func:`format_wall`
    — renders the same explicit fallback rather than a confirmation that
    looks successful but silently ate the poster's entire message.
    """
    remaining = format_remaining(post.expires_at)
    text = visible_text(post.text)
    chunks = wrap_cells(text, _ROW_TEXT_WIDTH, preserve_whitespace=True) or [""]
    body = "\n".join(ROW_PREFIX + chunk for chunk in chunks)
    return f"Wall posted ({remaining}):\n{body}"


def format_wall_status_line(post: WallPost) -> str:
    """Format the ``wall: sender: text (remaining)`` line shown by ``biff status``.

    Wraps the same way as :func:`format_wall` and :func:`format_wall_confirmation`
    — the body can run up to 512 chars and is potentially CJK/emoji-heavy —
    with continuation lines aligned under the sender label instead of
    embedding raw text on one unbounded line (biff-2sw). Falls back the same
    way on a control-only body.

    ``WallPost.from_user`` has no ``max_length``, so the sender is capped via
    :func:`sanitized_sender` the same way :func:`format_talk_line` caps its
    label — otherwise a forged sender collapses ``width`` toward 1 and
    explodes the wrap into one hard-broken line per glyph, each carrying a
    sender-sized indent (the same O(label x body) amplification
    :func:`format_talk_line` already guards against).

    The ``(remaining)`` suffix is reserved out of the wrap budget up front
    rather than appended to the last chunk after :func:`wrap_cells` has
    already chosen line breaks. An unbroken glyph run with no space to break
    at can fill the wrap budget exactly — appending the suffix afterward
    would then push that line past :data:`TABLE_WIDTH`, the exact overflow
    this function exists to prevent.
    """
    remaining = format_remaining(post.expires_at)
    sender = sanitized_sender(post.from_user)
    text = visible_text(post.text)
    prefix = f"wall: {sender}: "
    suffix = f" ({remaining})"
    width = max(TABLE_WIDTH - visible_width(prefix) - visible_width(suffix), 1)
    chunks = wrap_cells(text, width, preserve_whitespace=True) or [""]
    indent = " " * visible_width(prefix)
    rendered = [
        (prefix if i == 0 else indent) + chunk for i, chunk in enumerate(chunks)
    ]
    rendered[-1] += suffix
    return "\n".join(rendered)


# ---------------------------------------------------------------------------
# talk — conversation lines
# ---------------------------------------------------------------------------


def format_talk_line(label: str, body: str, *, stamp: str = "") -> list[str]:
    """Render one incoming talk line in the biff ``▶`` idiom, wrapped.

    Matches the ``who``/``read``/``wall`` convention: a leading ``▶``
    prefix, the sender's ``user:tty`` address, then the message — wrapped
    to :data:`TABLE_WIDTH` with continuation lines aligned under the body.
    *stamp* is the caller's ``[HH:MM] `` prefix (empty when timestamps are
    off).  All remote-controlled text is neutralised here via
    :func:`terminal_safe`, the output boundary (biff-lbj).

    Returns one string per rendered line so the caller can colourise each, or
    an empty list when *body* is empty to begin with — a truly bodiless frame
    (an accept, or an invite sent with no opening line) has nothing to say and
    renders no line.  A body that is merely *invisible* after neutralisation
    (whitespace-only or control-only) is a different case: the sender did send
    something, so the recipient sees an explanatory line rather than silence —
    see :func:`visible_text` (biff-7g7, biff-2sw round 6).

    The lead is bounded independently of the input: the label is truncated to
    :data:`_MAX_LABEL_WIDTH` and the continuation indent never exceeds
    :data:`TABLE_WIDTH`.  Without those caps a forged megabyte label would be
    copied onto every wrapped body chunk — O(label x body) allocation
    from a single frame (defense in depth behind the
    :meth:`TalkNotification.from_payload` boundary clamp).

    Precondition: *body* MUST already be boundary-clamped by the caller — the
    lead is capped here, but the body is not, so the wrap work is O(body).
    Every current caller passes the ≤ :data:`~biff.talk_types.MAX_BODY_LEN`
    ``TalkNotification.nbody``; a future caller feeding an unclamped body from a
    non-notification source would reintroduce the linear term.
    """
    if not body:
        return []
    safe_body = visible_text(body)
    safe_label = _truncate(terminal_safe(label), _MAX_LABEL_WIDTH)
    lead = f"{HEADER_PREFIX}{stamp}{safe_label}  "
    width = max(_TALK_WRAP_MIN, TABLE_WIDTH - visible_width(lead))
    # preserve_whitespace=True keeps the sender's spacing *within* a line —
    # the default collapse would rewrite each whitespace char to a space.
    # terminal_safe has already stripped every control char (tabs, newlines)
    # before this point, so only spaces remain: nothing can inject a line or skew
    # the wrap width.  wrap_cells measures by terminal cell (wcwidth), not code
    # points, so CJK/emoji in the body wrap at the right boundary instead of
    # overflowing TABLE_WIDTH.  Whitespace that lands on a wrap boundary is
    # still dropped — the render is not byte-for-byte verbatim across wraps,
    # and deliberately so: keeping boundary spaces would skew continuation
    # lines off the hang indent for no reader benefit.
    chunks = wrap_cells(safe_body, width, preserve_whitespace=True) or [""]
    indent = " " * min(visible_width(lead), TABLE_WIDTH)
    return [lead + chunks[0], *(indent + chunk for chunk in chunks[1:])]


def format_talk_echo(prefix: str, message: str) -> str:
    """Render *prefix* followed by *message* wrapped to table width.

    Shares :func:`format_wall_confirmation`'s wrap treatment: the ``talk``
    accept/send confirmations and the ``/plan`` set confirmation both quote
    back the caller's own text — up to :data:`~biff.talk_types.MAX_BODY_LEN`
    chars for talk, unbounded for a plan, potentially CJK/emoji-heavy — and
    must not embed it raw on one unbounded line.

    A non-empty *message* that has nothing visible to show — whitespace-only,
    or control/escape characters only — shows the same explicit fallback
    :func:`format_wall_confirmation` does, rather than a confirmation that
    looks successful but silently ate the caller's entire message.  See
    :func:`visible_text` for how the two cases are worded differently: a
    whitespace-only message was never actually stripped of anything, so it
    must not claim otherwise.  An empty *message* (e.g. clearing a plan) is
    left blank — that is the caller's deliberate intent, not a sanitization
    failure.
    """
    text = visible_text(message) if message else message
    chunks = wrap_cells(text, _ROW_TEXT_WIDTH, preserve_whitespace=True) or [""]
    body = "\n".join(ROW_PREFIX + chunk for chunk in chunks)
    return f"{prefix}\n{body}"


def format_talk_end(label: str) -> str:
    """Render a partner-hangup line in the ``▶`` idiom.

    The label is capped at :data:`_MAX_LABEL_WIDTH` like the
    :func:`format_talk_line` lead, so a forged sender label (bounded only by the
    :meth:`TalkNotification.from_payload` ingress clamp) cannot drive the hangup
    line past :data:`TABLE_WIDTH`.
    """
    safe_label = _truncate(terminal_safe(label), _MAX_LABEL_WIDTH)
    return f"{HEADER_PREFIX}{safe_label} has ended the conversation."


# ---------------------------------------------------------------------------
# /read — message inbox
# ---------------------------------------------------------------------------

READ_SPECS: list[ColumnSpec] = [
    ColumnSpec("FROM", min_width=10),
    ColumnSpec("DATE", min_width=16),
    ColumnSpec("MESSAGE", min_width=10, fixed=False),
]


def format_read_dual(
    human_user: str,
    human_msgs: list[Message],
    agent_user: str,
    agent_msgs: list[Message],
) -> str:
    """Format messages with per-identity section headers.

    ``Message.from_user``/``from_tty`` have no ``max_length`` on the wire.
    FROM is a *fixed* :data:`READ_SPECS` column and MESSAGE is the shared
    variable/wrap column, so the sender is routed through
    :func:`sanitized_address` for the same two-fold reason
    :func:`format_read` is: an unbounded value would widen FROM for every
    row in a section's table, and collapse the MESSAGE wrap budget shared
    by every message in that section.
    """
    sections: list[str] = []
    for user, msgs in ((human_user, human_msgs), (agent_user, agent_msgs)):
        if not msgs:
            continue
        rows: list[list[str]] = []
        for m in msgs:
            ts = m.timestamp.strftime("%a %b %d %H:%M")
            sender = sanitized_address(m.from_user, m.from_tty)
            rows.append([sender, ts, terminal_safe(m.body)])
        table = format_table(READ_SPECS, rows)
        # Indent the table under the section header: replace the
        # leading HEADER_PREFIX on the column-header line with
        # ROW_PREFIX so it aligns as a sub-table row.
        indented_table = ROW_PREFIX + table[len(HEADER_PREFIX) :]
        sections.append(f"{HEADER_PREFIX}{user}\n{indented_table}")
    return "\n\n".join(sections)


def format_read(messages: list[Message]) -> str:
    """Format messages in BSD ``from(1)`` style.

    The FROM column renders a copy-pasteable reply address:
    ``user:ttyNN`` when the sender's tty is known, ``user`` otherwise.

    ``Message.from_user``/``from_tty`` have no ``max_length`` on the wire.
    FROM is a *fixed* :data:`READ_SPECS` column (unbounded content widens
    it for every row) and MESSAGE is the table's variable/wrap column
    (unbounded FROM content shrinks the shared wrap budget toward its
    floor for every row) — :func:`sanitized_address` bounds both.
    """
    rows: list[list[str]] = []
    for m in messages:
        ts = m.timestamp.strftime("%a %b %d %H:%M")
        sender = sanitized_address(m.from_user, m.from_tty)
        rows.append([sender, ts, terminal_safe(m.body)])
    return format_table(READ_SPECS, rows)
