"""Tests for shared formatting helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from biff._formatting import (
    ColumnSpec,
    format_idle,
    format_table,
    last_component,
    visible_width,
)
from biff.formatting import format_read, format_read_dual
from biff.models import Message


class TestFormatIdle:
    def test_zero_minutes(self) -> None:
        now = datetime.now(UTC)
        assert format_idle(now) == "0m"

    def test_minutes(self) -> None:
        dt = datetime.now(UTC) - timedelta(minutes=45)
        assert format_idle(dt) == "45m"

    def test_hours(self) -> None:
        dt = datetime.now(UTC) - timedelta(hours=3, minutes=15)
        assert format_idle(dt) == "3h"

    def test_days(self) -> None:
        dt = datetime.now(UTC) - timedelta(days=2, hours=5)
        assert format_idle(dt) == "2d"

    def test_boundary_59_minutes(self) -> None:
        dt = datetime.now(UTC) - timedelta(minutes=59)
        assert format_idle(dt) == "59m"

    def test_boundary_1_hour(self) -> None:
        dt = datetime.now(UTC) - timedelta(hours=1)
        assert format_idle(dt) == "1h"

    def test_boundary_23_hours(self) -> None:
        dt = datetime.now(UTC) - timedelta(hours=23, minutes=59)
        assert format_idle(dt) == "23h"

    def test_boundary_1_day(self) -> None:
        dt = datetime.now(UTC) - timedelta(days=1)
        assert format_idle(dt) == "1d"

    def test_future_timestamp_returns_zero(self) -> None:
        dt = datetime.now(UTC) + timedelta(hours=1)
        assert format_idle(dt) == "0m"


class TestVisibleWidth:
    def test_plain_string(self) -> None:
        assert visible_width("hello") == 5

    def test_ansi_stripped(self) -> None:
        assert visible_width("\033[1;33mhello\033[0m") == 5

    def test_empty(self) -> None:
        assert visible_width("") == 0

    def test_multiple_ansi(self) -> None:
        assert visible_width("\033[31mred\033[0m \033[32mgreen\033[0m") == 9


class TestLastComponent:
    def test_deep_path(self) -> None:
        assert last_component("/home/kai/projects/biff") == "biff"

    def test_trailing_slash(self) -> None:
        assert last_component("/home/kai/biff/") == "biff"

    def test_single_component(self) -> None:
        assert last_component("/biff") == "biff"

    def test_dash_passthrough(self) -> None:
        assert last_component("-") == "-"

    def test_empty_passthrough(self) -> None:
        assert last_component("") == ""

    def test_root(self) -> None:
        assert last_component("/") == "/"

    def test_relative_path(self) -> None:
        assert last_component("projects/biff") == "biff"


class TestFormatTable:
    def _two_col_specs(self) -> list[ColumnSpec]:
        return [
            ColumnSpec("NAME", min_width=4),
            ColumnSpec("PLAN", min_width=5, fixed=False),
        ]

    def test_header_starts_with_marker(self) -> None:
        specs = self._two_col_specs()
        result = format_table(specs, [["@kai", "coding"]])
        assert result.startswith("\u25b6  ")

    def test_data_rows_start_with_three_spaces(self) -> None:
        specs = self._two_col_specs()
        lines = format_table(specs, [["@kai", "coding"]]).splitlines()
        assert lines[1].startswith("   ")

    def test_variable_column_wraps(self) -> None:
        specs = self._two_col_specs()
        long_plan = "word " * 30
        result = format_table(specs, [["@kai", long_plan.strip()]])
        lines = result.splitlines()
        assert len(lines) > 2  # header + at least 2 data lines

    def test_continuation_lines_indented_to_var_column(self) -> None:
        specs = self._two_col_specs()
        long_plan = "word " * 30
        result = format_table(specs, [["@kai", long_plan.strip()]])
        lines = result.splitlines()
        # Variable column offset: prefix(3) + NAME width(4) + sep(2) = 9
        var_offset = 3 + 4 + 2
        for line in lines[2:]:
            assert line.startswith(" " * var_offset)

    def test_empty_rows_returns_header_only(self) -> None:
        specs = self._two_col_specs()
        result = format_table(specs, [])
        assert "\n" not in result

    def test_right_aligned_column(self) -> None:
        specs = [
            ColumnSpec("DUR", min_width=6, align="right"),
            ColumnSpec("PLAN", min_width=5, fixed=False),
        ]
        result = format_table(specs, [["1:23", "coding"]])
        lines = result.splitlines()
        # "1:23" should be right-aligned in a 6-wide column
        assert "  1:23" in lines[1]

    def test_multiple_rows(self) -> None:
        specs = self._two_col_specs()
        rows = [["@kai", "fix auth"], ["@eric", "review PR"]]
        result = format_table(specs, rows)
        lines = result.splitlines()
        assert len(lines) == 3  # header + 2 rows

    def test_ansi_in_content_preserved(self) -> None:
        specs = self._two_col_specs()
        ansi_text = "\033[32mcoding\033[0m"
        result = format_table(specs, [["@kai", ansi_text]])
        assert ansi_text in result

    def test_ansi_aware_padding(self) -> None:
        """Fixed columns with ANSI content pad based on visible width."""
        specs = [
            ColumnSpec("NAME", min_width=8),
            ColumnSpec("PLAN", min_width=5, fixed=False),
        ]
        # "\033[32m@kai\033[0m" has visible width 4, string length 13
        ansi_name = "\033[32m@kai\033[0m"
        result = format_table(specs, [[ansi_name, "work"]])
        row = result.splitlines()[1]
        # The NAME cell should be padded to 8 visible chars, not 8 string chars
        # So there should be 4 trailing spaces after the ANSI reset
        assert f"{ansi_name}    " in row

    def test_header_column_names(self) -> None:
        specs = [
            ColumnSpec("NAME", min_width=4),
            ColumnSpec("TTY", min_width=3),
            ColumnSpec("PLAN", min_width=5, fixed=False),
        ]
        result = format_table(specs, [["@kai", "tty1", "work"]])
        header = result.splitlines()[0]
        assert "NAME" in header
        assert "TTY" in header
        assert "PLAN" in header

    def test_second_row_not_affected_by_first_wrap(self) -> None:
        specs = self._two_col_specs()
        long_plan = "word " * 30
        rows = [["@kai", long_plan.strip()], ["@eric", "short"]]
        result = format_table(specs, rows)
        lines = result.splitlines()
        # Find eric's row — should be a normal full row, not a continuation
        eric_lines = [ln for ln in lines if "@eric" in ln]
        assert len(eric_lines) == 1
        assert eric_lines[0].startswith("   ")

    def test_max_one_variable_column(self) -> None:
        specs = [
            ColumnSpec("A", min_width=4, fixed=False),
            ColumnSpec("B", min_width=4, fixed=False),
        ]
        with pytest.raises(ValueError, match="at most one"):
            format_table(specs, [["a", "b"]])

    def test_no_variable_column(self) -> None:
        specs = [
            ColumnSpec("A", min_width=4),
            ColumnSpec("B", min_width=4),
        ]
        result = format_table(specs, [["aa", "bb"]])
        assert "aa" in result
        assert "bb" in result

    def test_who_style_fits_80(self) -> None:
        """Realistic /who output stays within 80 columns."""
        specs = [
            ColumnSpec("NAME", min_width=4),
            ColumnSpec("TTY", min_width=3),
            ColumnSpec("IDLE", min_width=4),
            ColumnSpec("S", min_width=1),
            ColumnSpec("HOST", min_width=4),
            ColumnSpec("DIR", min_width=3),
            ColumnSpec("PLAN", min_width=10, fixed=False),
        ]
        rows = [
            [
                "@jmf-pobox",
                "tty4",
                "0m",
                "+",
                "m2-mb-air",
                "biff",
                "biff-xmv: Fix NATS consumer leak delete durable consumers",
            ],
        ]
        result = format_table(specs, rows)
        for line in result.splitlines():
            assert len(line) <= 80, f"Line exceeds 80 chars: {line!r}"


class TestFormatReadFromTty:
    """format_read renders a single FROM column with reply address (biff-q0mf)."""

    def test_from_column_includes_tty(self) -> None:
        msgs = [Message(from_user="kai", to_user="eric", body="hey", from_tty="tty1")]
        output = format_read(msgs)
        assert "FROM" in output
        assert "FROM_TTY" not in output
        assert "kai:tty1" in output
        assert "@kai" not in output

    def test_from_column_without_tty(self) -> None:
        msgs = [Message(from_user="kai", to_user="eric", body="hey", from_tty="")]
        output = format_read(msgs)
        assert "kai" in output
        assert "@kai" not in output
        # Should not have a colon after kai (no tty)
        lines = output.strip().split("\n")
        data_lines = [line for line in lines[1:] if "kai" in line]
        assert data_lines
        assert "kai:" not in data_lines[0]


class TestFormatReadDual:
    """format_read_dual renders per-identity sections with headers."""

    def _make_msg(
        self, from_user: str, to_user: str, body: str, from_tty: str = ""
    ) -> Message:
        return Message(
            from_user=from_user, to_user=to_user, body=body, from_tty=from_tty
        )

    def test_both_sections_rendered(self) -> None:
        human_msgs = [self._make_msg("kai", "jfreeman", "hey Jim", from_tty="tty2")]
        agent_msgs = [self._make_msg("rmh", "claude", "impl done", from_tty="tty3")]
        output = format_read_dual("jfreeman", human_msgs, "claude", agent_msgs)
        assert "\u25b6  jfreeman" in output
        assert "\u25b6  claude" in output
        assert "hey Jim" in output
        assert "impl done" in output

    def test_human_section_first(self) -> None:
        human_msgs = [self._make_msg("kai", "jfreeman", "for Jim")]
        agent_msgs = [self._make_msg("rmh", "claude", "for Claude")]
        output = format_read_dual("jfreeman", human_msgs, "claude", agent_msgs)
        human_pos = output.index("\u25b6  jfreeman")
        agent_pos = output.index("\u25b6  claude")
        assert human_pos < agent_pos

    def test_only_human_messages(self) -> None:
        human_msgs = [self._make_msg("kai", "jfreeman", "only human")]
        output = format_read_dual("jfreeman", human_msgs, "claude", [])
        assert "\u25b6  jfreeman" in output
        assert "claude" not in output
        assert "only human" in output

    def test_only_agent_messages(self) -> None:
        agent_msgs = [self._make_msg("rmh", "claude", "only agent")]
        output = format_read_dual("jfreeman", [], "claude", agent_msgs)
        assert "\u25b6  claude" in output
        assert "jfreeman" not in output
        assert "only agent" in output

    def test_column_headers_present_in_each_section(self) -> None:
        human_msgs = [self._make_msg("kai", "jfreeman", "hello")]
        agent_msgs = [self._make_msg("rmh", "claude", "world")]
        output = format_read_dual("jfreeman", human_msgs, "claude", agent_msgs)
        # Each section should have FROM/DATE/MESSAGE column headers
        assert output.count("FROM") == 2
        assert output.count("DATE") == 2
        assert output.count("MESSAGE") == 2

    def test_from_column_includes_tty(self) -> None:
        human_msgs = [self._make_msg("kai", "jfreeman", "hey", from_tty="tty2")]
        output = format_read_dual("jfreeman", human_msgs, "claude", [])
        assert "kai:tty2" in output

    def test_sections_separated_by_blank_line(self) -> None:
        human_msgs = [self._make_msg("kai", "jfreeman", "a")]
        agent_msgs = [self._make_msg("rmh", "claude", "b")]
        output = format_read_dual("jfreeman", human_msgs, "claude", agent_msgs)
        assert "\n\n" in output
