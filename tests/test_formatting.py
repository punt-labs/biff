"""Tests for the shared formatting module (biff.formatting).

Verifies domain-level format functions produce correct output.
These functions are shared by both MCP tools and CLI commands.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from biff._formatting import TABLE_WIDTH
from biff.formatting import (
    _TALK_WRAP_MIN,
    format_finger,
    format_finger_multi,
    format_last,
    format_read,
    format_talk_end,
    format_talk_line,
    format_wall,
    format_who,
    pair_events,
    parse_duration,
    sanitize_wall_message,
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


class TestFormatRead:
    def test_basic_messages(self):
        m = Message(from_user="kai", to_user="eric", body="hey there")
        result = format_read([m])
        assert "kai" in result
        assert "@kai" not in result
        assert "hey there" in result


class TestTerminalSafe:
    """`terminal_safe` strips control/escape chars from remote text (biff-lbj)."""

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
    """Remote terminal escapes are neutralized at every render site (biff-lbj).

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


class TestFormatTalkLine:
    """`format_talk_line` renders talk in the ▶ who/read/wall idiom (biff-7g7)."""

    def test_short_message_single_prefixed_line(self) -> None:
        assert format_talk_line("eric:tty2", "hi") == ["▶  eric:tty2  hi"]

    def test_no_tty_falls_back_to_user(self) -> None:
        assert format_talk_line("eric", "hi") == ["▶  eric  hi"]

    def test_timestamp_prefix_between_arrow_and_label(self) -> None:
        assert format_talk_line("eric:tty2", "hi", stamp="[14:32] ") == [
            "▶  [14:32] eric:tty2  hi"
        ]

    def test_empty_body_renders_nothing(self) -> None:
        assert format_talk_line("eric:tty2", "") == []

    def test_control_only_body_renders_nothing(self) -> None:
        # A body that is empty only AFTER neutralisation (control-only payload)
        # must produce no line — not a bare, dangling lead (biff-7g7).
        assert format_talk_line("eric:tty2", "\x00\x1b\x07") == []

    def test_whitespace_only_body_renders_nothing(self) -> None:
        # Spaces survive terminal_safe (they are printable), but a body with
        # nothing but whitespace has nothing to show — it must render no line,
        # not a bare ▶ lead (biff-7g7).
        assert format_talk_line("eric:tty2", "   ") == []
        assert format_talk_line("eric:tty2", "\t \n") == []

    def test_internal_space_runs_preserved(self) -> None:
        # The message is the user's content — runs of intentional spaces (aligned
        # text) must survive verbatim.  wrap(replace_whitespace=False) keeps them;
        # the default rewrites each whitespace char and can alter the body.
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
        # _MAX_LABEL_WIDTH as format_talk_line's lead (biff-7g7).
        out = format_talk_end("u" * 129)
        assert len(out) <= TABLE_WIDTH
        assert "…" in out
