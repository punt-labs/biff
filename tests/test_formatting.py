"""Tests for the shared formatting module (biff.formatting).

Verifies domain-level format functions produce correct output.
These functions are shared by both MCP tools and CLI commands.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from biff._formatting import TABLE_WIDTH, visible_width
from biff.formatting import (
    _MAX_LABEL_CHARS,
    _NO_PRINTABLE_TEXT,
    _NO_VISIBLE_CONTENT,
    _TALK_WRAP_MIN,
    format_dead_footnote,
    format_finger,
    format_finger_multi,
    format_last,
    format_read,
    format_talk_echo,
    format_talk_end,
    format_talk_line,
    format_user_header,
    format_wall,
    format_wall_confirmation,
    format_wall_status_line,
    format_who,
    pair_events,
    parse_duration,
    sanitize_wall_message,
    sanitized_address,
    sanitized_sender,
    terminal_safe,
)
from biff.models import Message, SessionEvent, UserSession, WallPost


class TestFormatWho:
    def test_single_session(self):
        session = UserSession(
            user="kai",
            tty="abcd1234",
            tty_name="tty1",
            plan="working on biff",
            last_active=datetime.now(UTC),
        )
        result = format_who([session])
        assert "kai" in result
        assert "tty1" in result
        assert "@kai" not in result  # no @ prefix

    def test_empty_sessions(self):
        result = format_who([])
        # Header-only table — no data rows, but no crash
        assert "NAME" in result
        assert "@" not in result

    def test_plan_flag_column(self):
        with_plan = UserSession(
            user="kai", tty="abcd1234", plan="coding", last_active=datetime.now(UTC)
        )
        without_plan = UserSession(
            user="eric", tty="efgh5678", last_active=datetime.now(UTC)
        )
        result = format_who([with_plan, without_plan])
        assert "P" in result.splitlines()[0]  # header
        lines = result.splitlines()
        # kai has a plan → "+"
        kai_line = next(line for line in lines if "kai" in line and "eric" not in line)
        eric_line = next(line for line in lines if "eric" in line)
        # P column is after S column; both have "+" for S (biff_enabled default)
        # so kai's line has two consecutive "+" (S and P), eric has "+" then "-"
        assert "+  +" in kai_line  # S=+, P=+
        assert "+  -" in eric_line  # S=+, P=-

    def test_giant_user_and_hostname_render_bounded(self) -> None:
        # UserSession.user/tty_name/hostname have no max_length on the wire,
        # and every WHO_SPECS column is fixed=True — format_table sizes a
        # fixed column to its longest cell across ALL rows, so one forged
        # session would otherwise widen NAME and HOST for every row in the
        # table, not just its own (mirrors the wall/talk giant-sender
        # defense-in-depth tests for format_wall/format_wall_status_line).
        forged = UserSession(
            user="u" * 10_000,
            tty="abcd1234",
            tty_name="t" * 10_000,
            hostname="h" * 10_000,
            last_active=datetime.now(UTC),
        )
        normal = UserSession(user="eric", tty="efgh5678", last_active=datetime.now(UTC))
        result = format_who([forged, normal])
        longest = max(visible_width(line) for line in result.splitlines())
        assert longest <= 2 * TABLE_WIDTH
        # The unforged row must not have been dragged along by the forged
        # session's column widths.
        eric_line = next(line for line in result.splitlines() if "eric" in line)
        assert visible_width(eric_line) <= 2 * TABLE_WIDTH


class TestFormatWhoKindTags:
    def test_agent_shows_tag(self):
        session = UserSession(
            user="claude",
            tty="abcd1234",
            tty_name="tty1",
            kind="agent",
            last_active=datetime.now(UTC),
        )
        result = format_who([session])
        assert "[A]" in result

    def test_human_no_tag(self):
        session = UserSession(
            user="kai",
            tty="abcd1234",
            tty_name="tty1",
            kind="human",
            last_active=datetime.now(UTC),
        )
        result = format_who([session])
        assert "[A]" not in result

    def test_empty_kind_no_tag(self):
        session = UserSession(
            user="kai",
            tty="abcd1234",
            tty_name="tty1",
            kind="",
            last_active=datetime.now(UTC),
        )
        result = format_who([session])
        assert "[A]" not in result


class TestFormatDeadFootnote:
    def test_empty_is_absent(self) -> None:
        assert format_dead_footnote([]) == ""

    def test_single_dead_session_singular_noun(self) -> None:
        dead = UserSession(
            user="ghost",
            tty="abcd1234",
            last_active=datetime.now(UTC) - timedelta(hours=12),
        )
        result = format_dead_footnote([dead])
        assert "1 session stopped responding (last seen 12h)" in result

    def test_multiple_dead_sessions_plural_noun_and_ages(self) -> None:
        now = datetime.now(UTC)
        recent = UserSession(
            user="recent-ghost", tty="a", last_active=now - timedelta(minutes=6)
        )
        stale = UserSession(
            user="old-ghost", tty="b", last_active=now - timedelta(days=35)
        )
        result = format_dead_footnote([recent, stale])
        assert "2 sessions stopped responding" in result
        assert "6m" in result
        assert "35d" in result

    def test_does_not_name_the_dead_sessions(self) -> None:
        """The footnote reports counts and ages only, never `user`/`tty_name`
        -- there is no fixed column here for an unbounded field to widen, so
        there is nothing to sanitize (DES-057)."""
        dead = UserSession(
            user="orphaned-agent",
            tty="a",
            last_active=datetime.now(UTC) - timedelta(hours=1),
        )
        result = format_dead_footnote([dead])
        assert "orphaned-agent" not in result


class TestFormatFinger:
    def test_single_session(self):
        session = UserSession(
            user="kai",
            tty="abcd1234",
            tty_name="main",
            plan="debugging relay",
            last_active=datetime.now(UTC),
        )
        result = format_finger(session)
        assert "Login: kai" in result
        assert "main" in result
        assert "debugging relay" in result

    def test_multi_session(self):
        s1 = UserSession(
            user="kai",
            tty="aaaa1111",
            tty_name="tty1",
            last_active=datetime.now(UTC),
        )
        s2 = UserSession(
            user="kai",
            tty="bbbb2222",
            tty_name="tty2",
            last_active=datetime.now(UTC) - timedelta(minutes=5),
        )
        result = format_finger_multi([s1, s2])
        assert "Login: kai" in result
        assert "tty1" in result
        assert "tty2" in result

    def test_finger_shows_kind(self):
        session = UserSession(
            user="claude",
            tty="abcd1234",
            tty_name="tty1",
            kind="agent",
            display_name="Claude Agento",
            last_active=datetime.now(UTC),
        )
        result = format_finger(session)
        assert "Login: claude [agent]" in result

    def test_finger_no_kind_when_empty(self):
        session = UserSession(
            user="kai",
            tty="abcd1234",
            tty_name="tty1",
            kind="",
            last_active=datetime.now(UTC),
        )
        result = format_finger(session)
        assert "Login: kai" in result
        assert "[" not in result.split("\n")[0]

    def test_cjk_plan_wraps_within_the_table_width(self) -> None:
        # /plan is free-form user text with no length cap — a CJK-heavy
        # plan renders at 2 cells/glyph and must wrap, not overflow.
        session = UserSession(
            user="kai",
            tty="abcd1234",
            tty_name="tty1",
            plan="这是一段很长的中文文本用来测试自动换行是否正常工作" * 3,
            last_active=datetime.now(UTC),
        )
        result = format_finger(session)
        lines = result.splitlines()
        assert len(lines) > 3  # header + on-since + Plan: + wrapped body
        for line in lines:
            assert visible_width(line) <= TABLE_WIDTH

    def test_cjk_host_and_dir_wrap_within_the_table_width(self) -> None:
        session = UserSession(
            user="kai",
            tty="abcd1234",
            tty_name="tty1",
            hostname="主机名" * 20,
            pwd="/项目/代码仓库" * 10,
            last_active=datetime.now(UTC),
        )
        result = format_finger(session)
        for line in result.splitlines():
            assert visible_width(line) <= TABLE_WIDTH

    def test_giant_user_kind_and_display_name_render_bounded(self) -> None:
        # UserSession.user/kind/display_name have no max_length on the
        # wire, and fmt_cell pads its column to width but never truncates
        # — an unbounded field previously rendered one unbounded Login:
        # line, however long the user/kind/display_name.
        session = UserSession(
            user="u" * 10_000,
            tty="abcd1234",
            tty_name="tty1",
            kind="k" * 10_000,
            display_name="d" * 10_000,
            last_active=datetime.now(UTC),
        )
        result = format_user_header(session)
        for line in result.splitlines():
            assert visible_width(line) <= 2 * TABLE_WIDTH


class TestParseDuration:
    def test_default_empty(self):
        assert parse_duration("") == timedelta(hours=1)

    def test_minutes(self):
        assert parse_duration("30m") == timedelta(minutes=30)

    def test_hours(self):
        assert parse_duration("2h") == timedelta(hours=2)

    def test_days(self):
        assert parse_duration("3d") == timedelta(days=3)


class TestSanitizeWallMessage:
    def test_strips_control_chars(self):
        result = sanitize_wall_message("hello\x00world")
        assert result == "helloworld"

    def test_collapses_whitespace(self):
        result = sanitize_wall_message("hello   world  foo")
        assert result == "hello world foo"


class TestFormatWall:
    def test_basic_wall(self):
        now = datetime.now(UTC)
        wall = WallPost(
            text="standup in 5",
            from_user="kai",
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        result = format_wall(wall)
        assert "WALL" in result
        assert "kai" in result
        assert "@kai" not in result
        assert "standup in 5" in result

    def test_cjk_body_wraps_within_the_table_width(self) -> None:
        # A CJK-heavy wall post (up to 512 chars) renders at 2 cells/glyph —
        # without cell-aware wrapping this overflows TABLE_WIDTH on one line.
        now = datetime.now(UTC)
        wall = WallPost(
            text="这是一段很长的中文文本用来测试自动换行是否正常工作" * 3,
            from_user="kai",
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        result = format_wall(wall)
        lines = result.splitlines()
        assert len(lines) > 2  # header + wrapped body lines
        for line in lines:
            assert visible_width(line) <= TABLE_WIDTH

    def test_emoji_body_wraps_within_the_table_width(self) -> None:
        now = datetime.now(UTC)
        wall = WallPost(
            text="🚀" * 90,
            from_user="kai",
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        result = format_wall(wall)
        for line in result.splitlines():
            assert visible_width(line) <= TABLE_WIDTH

    def test_control_only_body_renders_fallback(self) -> None:
        # WallPost.text requires min_length=1, so a control-only body still
        # passes validation but strips to nothing under terminal_safe — the
        # render must say so explicitly, not show a blank, empty-looking
        # WALL banner that hides the fact the message was dropped.
        now = datetime.now(UTC)
        wall = WallPost(
            text="\x00\x1b\x07",
            from_user="kai",
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        result = format_wall(wall)
        assert _NO_PRINTABLE_TEXT in result

    def test_whitespace_only_body_renders_distinct_fallback(self) -> None:
        # A body that is nothing but spaces survives terminal_safe unchanged
        # (spaces are printable) — it is not "stripped", so it must not get
        # the same wording as a control-only body that actually lost content.
        now = datetime.now(UTC)
        wall = WallPost(
            text="a   ",  # min_length=1 forbids pure whitespace; pad with "a"
            from_user="kai",
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        result = format_wall(wall)
        assert "a" in result
        assert _NO_PRINTABLE_TEXT not in result
        assert _NO_VISIBLE_CONTENT not in result

    def test_giant_sender_renders_bounded(self) -> None:
        # WallPost.from_user has no max_length — mirrors
        # test_giant_label_and_body_render_bounded's defense in depth for
        # format_talk_line: a forged sender must not blow the header line
        # (or, for format_wall_status_line, the per-line indent) past a
        # bounded size.
        now = datetime.now(UTC)
        wall = WallPost(
            text="word " * 100,  # 500 chars — WallPost caps text at 512
            from_user="u" * 10_000,
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        result = format_wall(wall)
        longest = max(visible_width(line) for line in result.splitlines())
        assert longest <= 2 * TABLE_WIDTH

    def test_giant_from_tty_renders_bounded(self) -> None:
        # WallPost.from_tty has no max_length either, and format_wall
        # concatenates it onto the already-clipped from_user
        # (``sender += f" ({from_tty})"``) — clipping from_user alone and
        # then appending an unbounded from_tty defeats the cap just as
        # completely as an unbounded from_user would.
        now = datetime.now(UTC)
        wall = WallPost(
            text="word " * 100,  # 500 chars — WallPost caps text at 512
            from_user="kai",
            from_tty="t" * 10_000,
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        result = format_wall(wall)
        longest = max(visible_width(line) for line in result.splitlines())
        assert longest <= 2 * TABLE_WIDTH


class TestFormatWallConfirmation:
    """The post-confirmation shown to the poster wraps like the read-back."""

    def test_basic_confirmation(self) -> None:
        now = datetime.now(UTC)
        post = WallPost(
            text="deploy freeze",
            from_user="kai",
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        result = format_wall_confirmation(post)
        assert "Wall posted" in result
        assert "deploy freeze" in result

    def test_cjk_body_wraps_within_the_table_width(self) -> None:
        now = datetime.now(UTC)
        post = WallPost(
            text="这是一段很长的中文文本用来测试自动换行是否正常工作" * 3,
            from_user="kai",
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        result = format_wall_confirmation(post)
        lines = result.splitlines()
        assert len(lines) > 2  # lead + wrapped body lines
        for line in lines:
            assert visible_width(line) <= TABLE_WIDTH

    def test_emoji_body_wraps_within_the_table_width(self) -> None:
        now = datetime.now(UTC)
        post = WallPost(
            text="🚀" * 90,
            from_user="kai",
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        result = format_wall_confirmation(post)
        for line in result.splitlines():
            assert visible_width(line) <= TABLE_WIDTH

    def test_control_only_body_renders_fallback(self) -> None:
        # A confirmation that quietly showed nothing would look successful
        # while hiding that the poster's entire message was dropped.
        now = datetime.now(UTC)
        post = WallPost(
            text="\x00\x1b\x07",
            from_user="kai",
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        result = format_wall_confirmation(post)
        assert _NO_PRINTABLE_TEXT in result

    def test_whitespace_only_body_renders_distinct_fallback(self) -> None:
        now = datetime.now(UTC)
        post = WallPost(
            text="a   ",
            from_user="kai",
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        result = format_wall_confirmation(post)
        assert "a" in result
        assert _NO_PRINTABLE_TEXT not in result
        assert _NO_VISIBLE_CONTENT not in result


class TestFormatWallStatusLine:
    """The ``biff status`` wall line wraps like every other wall render."""

    def test_basic_line(self) -> None:
        now = datetime.now(UTC)
        post = WallPost(
            text="deploy freeze",
            from_user="kai",
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        result = format_wall_status_line(post)
        assert result.startswith("wall: kai: deploy freeze (")

    def test_cjk_body_wraps_within_the_table_width(self) -> None:
        now = datetime.now(UTC)
        post = WallPost(
            text="这是一段很长的中文文本用来测试自动换行是否正常工作" * 3,
            from_user="kai",
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        result = format_wall_status_line(post)
        lines = result.splitlines()
        assert len(lines) > 1
        for line in lines:
            assert visible_width(line) <= TABLE_WIDTH

    def test_escapes_neutralized(self) -> None:
        now = datetime.now(UTC)
        post = WallPost(
            from_user="ev\x1b[2Kil",
            text="dep\x1b[2Jloy freeze",
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        out = format_wall_status_line(post)
        assert "\x1b[2J" not in out
        assert "\x1b[2K" not in out
        assert "dep[2Jloy freeze" in out

    def test_control_only_body_renders_fallback(self) -> None:
        now = datetime.now(UTC)
        post = WallPost(
            text="\x00\x1b\x07",
            from_user="kai",
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        result = format_wall_status_line(post)
        assert _NO_PRINTABLE_TEXT in result

    def test_whitespace_only_body_renders_distinct_fallback(self) -> None:
        now = datetime.now(UTC)
        post = WallPost(
            text="a   ",
            from_user="kai",
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        result = format_wall_status_line(post)
        assert "a" in result
        assert _NO_PRINTABLE_TEXT not in result
        assert _NO_VISIBLE_CONTENT not in result

    def test_giant_sender_renders_bounded(self) -> None:
        # WallPost.from_user has no max_length. Here the sender feeds
        # directly into the wrap-width budget (``width = TABLE_WIDTH -
        # visible_width(prefix)``), so an uncapped sender collapses width
        # toward 1 and explodes into one hard-broken line per glyph, each
        # carrying a sender-sized indent — the O(label x body) amplification
        # test_giant_label_and_body_render_bounded guards against for talk.
        now = datetime.now(UTC)
        post = WallPost(
            text="word " * 100,  # 500 chars — WallPost caps text at 512
            from_user="u" * 10_000,
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        result = format_wall_status_line(post)
        longest = max(visible_width(line) for line in result.splitlines())
        assert longest <= 2 * TABLE_WIDTH

    def test_unbroken_body_suffix_never_overflows_table_width(self) -> None:
        # An unbroken run (no spaces to break at) hard-wraps to chunks that
        # exactly fill the wrap budget. Appending " (remaining)" to the last
        # chunk AFTER wrap_cells had already chosen line breaks (the old
        # logic) pushed that line past TABLE_WIDTH — the suffix must be
        # reserved out of the budget up front instead.
        now = datetime.now(UTC)
        prefix_width = visible_width("wall: kai: ")
        body_width = TABLE_WIDTH - prefix_width
        post = WallPost(
            text="x" * (body_width * 3),
            from_user="kai",
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        result = format_wall_status_line(post)
        for line in result.splitlines():
            assert visible_width(line) <= TABLE_WIDTH


class TestFormatTalkEcho:
    """``talk`` accept/send confirmations wrap the echoed message."""

    def test_basic_echo(self) -> None:
        result = format_talk_echo("Sent to eric:tty2:", "hello there")
        assert result == "Sent to eric:tty2:\n   hello there"

    def test_cjk_message_wraps_within_the_table_width(self) -> None:
        message = "这是一段很长的中文文本用来测试自动换行是否正常工作" * 3
        result = format_talk_echo("Sent:", message)
        lines = result.splitlines()
        assert len(lines) > 2  # prefix + wrapped body lines
        for line in lines:
            assert visible_width(line) <= TABLE_WIDTH

    def test_emoji_message_wraps_within_the_table_width(self) -> None:
        result = format_talk_echo("Sent:", "🚀" * 90)
        for line in result.splitlines():
            assert visible_width(line) <= TABLE_WIDTH

    def test_escapes_neutralized(self) -> None:
        result = format_talk_echo("Sent:", "hi\x1b[2Jthere")
        assert "\x1b[2J" not in result
        assert "hi[2Jthere" in result

    def test_control_only_body_renders_fallback(self) -> None:
        # A non-empty message that strips to nothing must say so explicitly
        # instead of echoing a blank body that looks like a successful,
        # empty send (this helper is also reused by /plan's confirmation).
        result = format_talk_echo("Sent:", "\x00\x1b\x07")
        assert _NO_PRINTABLE_TEXT in result

    def test_whitespace_only_message_renders_distinct_fallback(self) -> None:
        # "   " survives terminal_safe unchanged (spaces are printable), so
        # claiming it was "stripped to no printable text" would be false —
        # nothing was stripped.  The state/confirmation split this closes:
        # /plan "   " stores plan="   " verbatim (update_current_session uses
        # model_copy, which skips pydantic validation) while the confirmation
        # must not claim the text was removed when it wasn't.
        result = format_talk_echo("Plan:", "   ")
        assert _NO_PRINTABLE_TEXT not in result
        assert _NO_VISIBLE_CONTENT in result

    def test_mixed_control_and_space_message_renders_distinct_fallback(
        self,
    ) -> None:
        # " \x07 " has a BEL sandwiched between two spaces.  terminal_safe
        # strips the BEL but keeps both spaces, so something survived (just
        # not anything visible) — this must read the same as a pure-
        # whitespace body, not "no printable text", which would falsely
        # claim nothing survived at all.
        result = format_talk_echo("Plan:", " \x07 ")
        assert _NO_PRINTABLE_TEXT not in result
        assert _NO_VISIBLE_CONTENT in result

    def test_combining_marks_only_message_renders_distinct_fallback(
        self,
    ) -> None:
        # Three bare combining acute accents, no base character.  Combining
        # marks (Unicode category Mn/Mc/Me) are str.isprintable() == True, so
        # terminal_safe leaves them untouched, and they are not whitespace,
        # so .strip() wouldn't remove them either — a naive truthiness check
        # on the stripped string would wrongly call this visible content. It
        # occupies zero terminal cells (visible_width == 0): no base glyph to
        # attach to.
        result = format_talk_echo("Plan:", "́́́")
        assert _NO_PRINTABLE_TEXT not in result
        assert _NO_VISIBLE_CONTENT in result

    def test_variation_selector_only_message_renders_distinct_fallback(
        self,
    ) -> None:
        # A bare variation selector (U+FE0F, VARIATION SELECTOR-16) modifies
        # the presentation of a preceding base character; alone it renders
        # nothing and occupies zero terminal cells, but it is printable and
        # not whitespace.
        result = format_talk_echo("Plan:", "️")
        assert _NO_PRINTABLE_TEXT not in result
        assert _NO_VISIBLE_CONTENT in result

    def test_spacing_combining_mark_only_message_renders_distinct_fallback(
        self,
    ) -> None:
        # U+0940 DEVANAGARI VOWEL SIGN II is Unicode category Mc ("Spacing
        # Combining Mark") — the category used for Indic-script vowel signs
        # and visargas.  Like the Mn accents above it is str.isprintable()
        # and not whitespace, so it survives terminal_safe and .strip()
        # unchanged, but with no base consonant to attach to it renders no
        # glyph and occupies zero terminal cells.
        result = format_talk_echo("Plan:", "ी")
        assert _NO_PRINTABLE_TEXT not in result
        assert _NO_VISIBLE_CONTENT in result

    def test_isolated_hangul_jamo_only_message_renders_distinct_fallback(
        self,
    ) -> None:
        # U+1161 HANGUL JUNGSEONG A is a conjoining medial vowel jamo,
        # plausible from partial IME input or clipboard mangling.  Isolated
        # (not composed into a syllable block), wcwidth reports it as zero
        # width — printable, not whitespace, no visible glyph on its own.
        result = format_talk_echo("Plan:", "ᅡ")
        assert _NO_PRINTABLE_TEXT not in result
        assert _NO_VISIBLE_CONTENT in result

    def test_empty_message_stays_blank(self) -> None:
        # An empty message is a deliberate "nothing to say" (e.g. clearing a
        # plan via /plan ""), not a sanitization failure — it must not be
        # mistaken for control-only garbage and get the fallback text.
        result = format_talk_echo("Plan:", "")
        assert _NO_PRINTABLE_TEXT not in result
        assert result == "Plan:\n   "


class TestPairEvents:
    def test_pairs_login_logout(self):
        now = datetime.now(UTC)
        login = SessionEvent(
            session_key="kai:tty1",
            event="login",
            user="kai",
            timestamp=now - timedelta(hours=1),
        )
        logout = SessionEvent(
            session_key="kai:tty1",
            event="logout",
            user="kai",
            timestamp=now,
        )
        pairs = pair_events([login, logout])
        assert len(pairs) == 1
        assert pairs[0][0] == login
        assert pairs[0][1] == logout

    def test_unpaired_login(self):
        now = datetime.now(UTC)
        login = SessionEvent(
            session_key="kai:tty1",
            event="login",
            user="kai",
            timestamp=now,
        )
        pairs = pair_events([login])
        assert len(pairs) == 1
        assert pairs[0][1] is None


class TestFormatLast:
    def test_giant_user_and_hostname_render_bounded(self) -> None:
        # SessionEvent.user/tty_name/hostname have no max_length on the
        # wire, and every LAST_SPECS column is fixed=True — format_table
        # sizes a fixed column to its longest cell across ALL rows, so one
        # forged login event would otherwise widen NAME and HOST for every
        # row in the table, not just its own.
        now = datetime.now(UTC)
        forged = SessionEvent(
            session_key="evil:tty9",
            event="login",
            user="u" * 10_000,
            tty="tty9",
            tty_name="t" * 10_000,
            hostname="h" * 10_000,
            timestamp=now,
        )
        normal = SessionEvent(
            session_key="kai:tty1",
            event="login",
            user="kai",
            tty="tty1",
            timestamp=now,
        )
        result = format_last(
            [(forged, None), (normal, None)], {"evil:tty9", "kai:tty1"}
        )
        longest = max(visible_width(line) for line in result.splitlines())
        assert longest <= 2 * TABLE_WIDTH
        # The unforged row must not have been dragged along by the forged
        # event's column widths.
        kai_line = next(line for line in result.splitlines() if "kai" in line)
        assert visible_width(kai_line) <= 2 * TABLE_WIDTH


class TestFormatRead:
    def test_basic_messages(self):
        m = Message(from_user="kai", to_user="eric", body="hey there")
        result = format_read([m])
        assert "kai" in result
        assert "@kai" not in result
        assert "hey there" in result

    def test_leading_count_matches_message_count(self) -> None:
        # biff-9cz: the count reported to the caller must come from the
        # same list the table renders, not a separately polled summary
        # that can be stale relative to this fetch.
        messages = [
            Message(from_user="kai", to_user="eric", body="one"),
            Message(from_user="rmh", to_user="eric", body="two"),
            Message(from_user="alpha", to_user="eric", body="three"),
        ]
        result = format_read(messages)
        assert result.startswith("▶  3 new messages")
        data_lines = [line for line in result.splitlines() if "one" in line]
        assert len(data_lines) == 1

    def test_leading_count_singular_noun(self) -> None:
        m = Message(from_user="kai", to_user="eric", body="hey there")
        result = format_read([m])
        assert result.startswith("▶  1 new message\n")
        assert "1 new messages" not in result

    def test_giant_sender_renders_bounded(self) -> None:
        # Message.from_user/from_tty have no max_length on the wire. FROM
        # is a fixed READ_SPECS column (widens for every row) and MESSAGE
        # is the shared variable/wrap column (its budget shrinks toward its
        # floor for every row) — mirrors the wall/talk giant-sender
        # defense-in-depth tests for format_wall/format_wall_status_line.
        messages = [
            Message(
                from_user="u" * 10_000,
                from_tty="t" * 10_000,
                to_user="eric",
                body="hey there",
            ),
            Message(from_user="kai", to_user="eric", body="short reply"),
        ]
        result = format_read(messages)
        longest = max(visible_width(line) for line in result.splitlines())
        assert longest <= 2 * TABLE_WIDTH
        kai_line = next(line for line in result.splitlines() if "kai" in line)
        assert visible_width(kai_line) <= 2 * TABLE_WIDTH


class TestTerminalSafe:
    """`terminal_safe` strips control/escape chars from remote text."""

    def test_strips_esc_and_bel(self) -> None:
        assert terminal_safe("a\x1b[2Jb\x07c") == "a[2Jbc"

    def test_strips_newline_and_cr(self) -> None:
        # A single-line render must not be splittable by embedded newlines.
        assert terminal_safe("line1\nline2\rline3") == "line1line2line3"

    def test_preserves_printable_unicode(self) -> None:
        assert terminal_safe("kai:tty2 ▶ 🚀 café") == "kai:tty2 ▶ 🚀 café"

    def test_empty_string(self) -> None:
        assert terminal_safe("") == ""


class TestRenderSanitization:
    """Remote terminal escapes are neutralized at every render site.

    Each render path is fed a relay-sourced field carrying a screen-clear
    (`\\x1b[2J`) and assert the raw escape never reaches the output.
    """

    def test_read_body_and_sender(self) -> None:
        m = Message(from_user="e\x1b[2Kvil", to_user="kai", body="hi\x1b[2Jthere")
        out = format_read([m])
        assert "\x1b[2J" not in out
        assert "\x1b[2K" not in out
        assert "hi[2Jthere" in out

    def test_wall_text_and_sender(self) -> None:
        wall = WallPost(
            from_user="ev\x1b[2Kil",
            from_tty="tty\x1b[2K9",
            text="dep\x1b[2Jloy freeze",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        out = format_wall(wall)
        assert "\x1b[2J" not in out
        assert "\x1b[2K" not in out
        assert "dep[2Jloy freeze" in out

    def test_who_name_and_host(self) -> None:
        s = UserSession(user="ka\x1b[2Ji", tty="abc12345", hostname="ho\x1b[2Kst")
        out = format_who([s])
        assert "\x1b[2J" not in out
        assert "\x1b[2K" not in out

    def test_finger_plan_host_pwd(self) -> None:
        s = UserSession(
            user="kai",
            tty="abc12345",
            plan="pl\x1b[2Jan",
            hostname="h\x1b[2Kost",
            pwd="/co\x1b[2Kde",
        )
        out = format_finger(s)
        assert "\x1b[2J" not in out
        assert "\x1b[2K" not in out
        assert "pl[2Jan" in out

    def test_last_user_and_host(self) -> None:
        login = SessionEvent(
            session_key="evil:tty9",
            event="login",
            user="ev\x1b[2Kil",
            tty="tty9",
            hostname="h\x1b[2Kost",
        )
        out = format_last([(login, None)], {"evil:tty9"})
        assert "\x1b[2K" not in out
        assert re.search(r"ev\[2Kil", out) is not None


class TestSanitizedLabelCharBound:
    """clip_to_width alone is not enough to bound a sender label.

    ``clip_to_width`` only clips when ``visible_width(text)`` exceeds the
    cell-width budget. A label built entirely of combining marks or
    variation selectors has ``visible_width == 0`` no matter how many
    characters it holds — the cell-width axis never triggers, and an
    unbounded payload sails through verbatim. ``_sanitized_label`` must
    also bound raw character count, independent of cell width, to close
    that gap.
    """

    def test_combining_marks_sender_stays_bounded_in_length(self) -> None:
        # Thousands of bare combining acute accents: zero terminal cells,
        # but 10,000 characters if left uncapped.
        payload = "́" * 10_000
        out = sanitized_sender(payload)
        assert len(out) <= _MAX_LABEL_CHARS

    def test_combining_marks_address_stays_bounded_in_length(self) -> None:
        payload = "́" * 10_000
        out = sanitized_address(payload)
        assert len(out) <= _MAX_LABEL_CHARS

    def test_variation_selectors_sender_stays_bounded_in_length(self) -> None:
        # Variation selector-16 is printable and zero-width without a
        # preceding base glyph to attach to.
        payload = "️" * 10_000
        out = sanitized_sender(payload)
        assert len(out) <= _MAX_LABEL_CHARS

    def test_zero_width_payload_in_tty_field_stays_bounded(self) -> None:
        # The tty field is concatenated after from_user — must be bounded
        # the same way, not just the leading field.
        out = sanitized_sender("kai", "́" * 10_000)
        assert len(out) <= _MAX_LABEL_CHARS

    def test_ordinary_label_is_unaffected(self) -> None:
        assert sanitized_sender("kai", "tty1") == "kai (tty1)"
        assert sanitized_address("kai", "tty1") == "kai:tty1"


class TestFormatTalkLine:
    """`format_talk_line` renders talk in the ▶ who/read/wall idiom."""

    def test_short_message_single_prefixed_line(self) -> None:
        assert format_talk_line("eric:tty2", "hi") == ["▶  eric:tty2  hi"]

    def test_no_tty_falls_back_to_user(self) -> None:
        assert format_talk_line("eric", "hi") == ["▶  eric  hi"]

    def test_timestamp_prefix_between_arrow_and_label(self) -> None:
        assert format_talk_line("eric:tty2", "hi", stamp="[14:32] ") == [
            "▶  [14:32] eric:tty2  hi"
        ]

    def test_empty_body_renders_nothing(self) -> None:
        # A truly bodiless frame (nothing sent at all) has nothing to report.
        assert format_talk_line("eric:tty2", "") == []

    def test_control_only_body_renders_fallback(self) -> None:
        # A body that is empty only AFTER neutralisation (control-only
        # payload) still arrived — the recipient must see that, not silence.
        (line,) = format_talk_line("eric:tty2", "\x00\x1b\x07")
        assert "▶  eric:tty2  " in line
        assert "no printable text" in line

    def test_whitespace_only_body_renders_distinct_fallback(self) -> None:
        # Spaces survive terminal_safe unchanged (they are printable) — nothing
        # was stripped, so the fallback wording must not claim it was.
        (line,) = format_talk_line("eric:tty2", "   ")
        assert "no visible content" in line
        assert "no printable text" not in line

    def test_tab_and_newline_only_body_renders_stripped_fallback(self) -> None:
        # Tab and newline are NOT printable (unlike space) — terminal_safe
        # actually removes them, so this is the "stripped" case, not the
        # "untouched whitespace" case.
        (line,) = format_talk_line("eric:tty2", "\t\n")
        assert "no printable text" in line

    def test_internal_space_runs_preserved(self) -> None:
        # The message is the user's content — runs of intentional spaces (aligned
        # text) must survive verbatim.  wrap_cells(preserve_whitespace=True) keeps
        # them; the default collapses each whitespace run to a single space.
        assert format_talk_line("eric:tty2", "a    b   c") == [
            "▶  eric:tty2  a    b   c"
        ]

    def test_giant_label_and_body_render_bounded(self) -> None:
        # Defense in depth for the O(label x body) amplification: even if a
        # forged megabyte label/body slips past the boundary clamp, the render
        # must stay bounded — no line carries the raw label or a label-sized
        # indent, and the line count is bounded by the body, not the label.
        label = "u" * 10_000
        body = "word " * 2_000  # 10_000 chars
        lines = format_talk_line(label, body)
        longest = max(len(line) for line in lines)
        assert longest <= 2 * TABLE_WIDTH  # no O(label) line
        total = sum(len(line) for line in lines)
        assert total <= 2 * TABLE_WIDTH * len(lines)  # O(lines), not O(label x body)
        assert len(lines) <= len(body) // _TALK_WRAP_MIN + 2  # bounded by body/width

    def test_long_body_wraps_within_the_table_width(self) -> None:
        body = "word " * 40
        lines = format_talk_line("eric:tty2", body.strip())
        assert len(lines) > 1
        assert all(len(line) <= TABLE_WIDTH for line in lines)

    def test_continuation_aligns_under_the_body(self) -> None:
        lines = format_talk_line("eric:tty2", "alpha " * 40)
        # Body starts after "▶  eric:tty2  " — 14 visible columns.
        assert lines[0].startswith("▶  eric:tty2  ")
        assert lines[1].startswith(" " * 14)
        assert lines[1][14] != " "

    def test_escape_in_body_neutralized(self) -> None:
        (line,) = format_talk_line("eric:tty2", "clear\x1b[2Jme")
        assert "\x1b[2J" not in line
        assert "clear[2Jme" in line

    def test_escape_in_label_neutralized(self) -> None:
        (line,) = format_talk_line("e\x1b[2Jvil:tty2", "hi")
        assert "\x1b[2J" not in line
        assert "e[2Jvil:tty2  hi" in line

    def test_cjk_body_wraps_within_the_table_width(self) -> None:
        # A run of CJK glyphs renders at 2 cells each — this body is 60
        # code points but 120 terminal cells, so it must wrap into several
        # lines, each still within TABLE_WIDTH *cells*.
        body = "这是一段很长的中文文本用来测试自动换行是否正常工作" * 2
        lines = format_talk_line("eric:tty2", body)
        assert len(lines) > 1
        for line in lines:
            assert visible_width(line) <= TABLE_WIDTH

    def test_emoji_body_wraps_within_the_table_width(self) -> None:
        body = "🚀" * 60  # 60 emoji at 2 cells each = 120 cells
        lines = format_talk_line("eric:tty2", body)
        assert len(lines) > 1
        for line in lines:
            assert visible_width(line) <= TABLE_WIDTH

    def test_mixed_ascii_and_cjk_body_stays_within_width(self) -> None:
        body = ("hello 你好嗎 world 再見 ") * 6
        lines = format_talk_line("eric:tty2", body.strip())
        for line in lines:
            assert visible_width(line) <= TABLE_WIDTH


class TestFormatTalkEnd:
    def test_hangup_line_uses_the_arrow_prefix(self) -> None:
        expected = "▶  eric:tty2 has ended the conversation."
        assert format_talk_end("eric:tty2") == expected

    def test_escape_in_label_neutralized(self) -> None:
        out = format_talk_end("e\x1b[2Jvil")
        assert "\x1b[2J" not in out
        assert out == "▶  e[2Jvil has ended the conversation."

    def test_long_label_is_truncated(self) -> None:
        # A forged label (up to the from_payload MAX_KEY_LEN clamp) must not
        # produce an unbounded hangup line — the label is capped to the same
        # _MAX_LABEL_WIDTH as format_talk_line's lead.
        out = format_talk_end("u" * 129)
        assert len(out) <= TABLE_WIDTH
        assert "…" in out

    def test_long_cjk_label_is_clipped_by_cell_width(self) -> None:
        # A CJK label at 2 cells/glyph must clip by cell width, not code
        # points, or the hangup line overflows TABLE_WIDTH cells.
        out = format_talk_end("你" * 60)
        assert visible_width(out) <= TABLE_WIDTH
        assert "…" in out
