"""Tests for shared formatting helpers."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from biff._formatting import (
    ColumnSpec,
    clip_to_width,
    format_idle,
    format_table,
    last_component,
    visible_width,
    wrap_cells,
)
from biff.ethos_team import RoleEntry
from biff.formatting import (
    format_finger,
    format_finger_multi,
    format_read,
    format_who,
    who_mgr,
)
from biff.identity import git_user_name
from biff.models import Message, UserSession


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

    def test_cjk_counts_two_cells_per_glyph(self) -> None:
        # Each CJK ideograph renders at 2 terminal cells, not 1 code point.
        assert visible_width("你好") == 4

    def test_emoji_counts_two_cells(self) -> None:
        assert visible_width("🚀") == 2

    def test_mixed_ascii_and_cjk(self) -> None:
        # 5 ASCII cells + 3 CJK glyphs * 2 cells = 11.
        assert visible_width("hello你好嗎") == 11

    def test_control_character_contributes_zero_width(self) -> None:
        # wcwidth returns -1 for control/indeterminate characters; _char_width
        # clamps that to 0 rather than letting it subtract from the budget.
        # BEL (\x07) is outside the ANSI CSI regex, so it reaches wcwidth
        # directly, unlike stripped CSI sequences.
        assert visible_width("a\x07b") == 2


class TestWrapCells:
    def test_ascii_wraps_like_textwrap(self) -> None:
        lines = wrap_cells("word " * 20, 40)
        assert all(visible_width(line) <= 40 for line in lines)
        assert len(lines) > 1

    def test_cjk_wraps_by_cell_width_not_code_points(self) -> None:
        # 40 CJK glyphs at 2 cells each = 80 cells — must wrap well
        # before 80 code points if the budget is 40 cells.
        body = "你好" * 20  # "你好" x 20 == 40 glyphs, 80 cells
        lines = wrap_cells(body, 40)
        assert len(lines) > 1
        for line in lines:
            assert visible_width(line) <= 40

    def test_mixed_ascii_and_cjk_stays_within_budget(self) -> None:
        body = ("hello " + "你好嗎 ") * 10
        lines = wrap_cells(body, 30)
        for line in lines:
            assert visible_width(line) <= 30

    def test_preserve_whitespace_keeps_internal_runs(self) -> None:
        lines = wrap_cells("a    b   c", 40, preserve_whitespace=True)
        assert lines == ["a    b   c"]

    def test_collapses_whitespace_by_default(self) -> None:
        lines = wrap_cells("a\t\tb", 40)
        assert lines == ["a b"]

    def test_empty_text_returns_no_lines(self) -> None:
        assert wrap_cells("", 40) == []

    def test_never_drops_a_lone_wide_glyph_word(self) -> None:
        # A single unbroken run of wide glyphs longer than the budget must
        # still hard-break rather than overflow a line forever.
        body = "你" * 30  # 30 CJK glyphs, 60 cells, no whitespace
        lines = wrap_cells(body, 10)
        assert len(lines) > 1
        for line in lines:
            assert visible_width(line) <= 10
        assert "".join(lines) == body

    def test_single_glyph_wider_than_budget_still_emitted_whole(self) -> None:
        # A 2-cell glyph against a 1-cell budget has no narrower unit to
        # split into — it is emitted whole, exceeding the requested width,
        # rather than dropped or corrupted.
        assert wrap_cells("你a", 1) == ["你", "a"]

    def test_oversized_ansi_token_hard_break_never_splits_mid_escape(self) -> None:
        # A single unbroken "word" carrying an ANSI escape, wider than the
        # budget, must hard-break on visible content only — never inside
        # the escape sequence itself (e.g. "\x1b[31m" split into "\x1b[3"
        # and "1m" across two chunks, corrupting it). The escape is
        # stripped up front, the same way visible_width strips it, so no
        # chunk carries a partial or complete escape sequence at all.
        token = "\x1b[31m" + ("x" * 20) + "\x1b[0m"
        lines = wrap_cells(token, 5)
        assert len(lines) > 1
        assert "".join(lines) == "x" * 20
        for line in lines:
            assert "\x1b" not in line
            assert visible_width(line) <= 5
            assert visible_width(line) == len(line)


class TestClipToWidth:
    def test_ascii_under_budget_unchanged(self) -> None:
        assert clip_to_width("hello", 10) == "hello"

    def test_ascii_over_budget_clips_with_ellipsis(self) -> None:
        result = clip_to_width("hello world", 8)
        assert result.endswith("…")
        assert visible_width(result) <= 8

    def test_cjk_clips_by_cell_width_not_code_points(self) -> None:
        # 10 CJK glyphs = 20 cells; clipping to 8 cells must keep far
        # fewer than 8 code points, unlike a len()-based clip.
        text = "你" * 10
        result = clip_to_width(text, 8)
        assert visible_width(result) <= 8
        assert result.endswith("…")

    def test_zero_width_that_needs_clipping_raises(self) -> None:
        # A budget too narrow to even hold the ellipsis must raise rather
        # than silently return a result wider than the requested budget.
        with pytest.raises(ValueError, match="narrower than"):
            clip_to_width("hello", 0)

    def test_width_narrower_than_ellipsis_raises(self) -> None:
        with pytest.raises(ValueError, match="narrower than"):
            clip_to_width("hello world", 0)

    def test_width_meeting_the_ellipsis_budget_does_not_raise(self) -> None:
        # width == visible_width(ellipsis) is the minimum viable budget —
        # the clip degenerates to the bare ellipsis, but that's still a
        # result no wider than requested.
        result = clip_to_width("hello world", 1)
        assert result == "…"

    def test_text_that_fits_is_returned_even_when_width_is_zero(self) -> None:
        # No clipping needed — the ellipsis-vs-width contract only applies
        # when a clip actually has to happen.
        assert clip_to_width("", 0) == ""

    def test_ansi_text_that_needs_clipping_never_leaves_dangling_escape(self) -> None:
        # The truncation walk must strip ANSI first (matching visible_width),
        # not walk the raw string — otherwise a clip boundary lands inside
        # an escape sequence and emits a dangling, unterminated introducer
        # ahead of the ellipsis.
        text = "\x1b[31m" + ("x" * 20) + "\x1b[0m"
        result = clip_to_width(text, 8)
        assert result.endswith("…")
        assert "\x1b" not in result
        assert visible_width(result) <= 8


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
    """format_read renders a single FROM column with reply address."""

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


class TestWhoMgrColumn:
    def _row(
        self,
        launched_by: str = "",
        user: str = "claude",
        kind: str = "agent",
        owner_git: str = "",
    ) -> UserSession:
        return UserSession(
            user=user,
            tty="abcd1234",
            tty_name="tty9",
            kind=kind,
            repo="biff",
            hostname="h",
            launched_by=launched_by,
            owner_git=owner_git,
            last_active=datetime.now(UTC),
            last_tool_at=datetime.now(UTC),
        )

    def test_agent_mgr_is_the_rows_owner_git(self) -> None:
        lines = format_who([self._row("osjim", owner_git="jmf-pobox")]).splitlines()
        assert lines[0].split()[1:] == [
            "NAME",
            "K",
            "MGR",
            "REPO",
            "IDLE",
            "S",
            "P",
            "HOST",
        ]
        assert lines[1].split() == [
            "claude:tty9",
            "[A]",
            "jmf-pobox",
            "biff",
            "0m",
            "+",
            "-",
            "h",
        ]

    def test_each_row_uses_its_own_owner_not_the_viewers(self) -> None:
        # Regression: a viewer-side git name was applied to every row.
        a = self._row(user="alpha", owner_git="alice")
        b = self._row(user="beta", owner_git="bob")
        assert who_mgr(a) == "alice"
        assert who_mgr(b) == "bob"
        out = format_who([a, b])
        assert "alice" in out
        assert "bob" in out

    def test_owner_git_equal_to_self_falls_to_os_user(self) -> None:
        assert who_mgr(self._row("jim", owner_git="claude")) == "jim"

    def test_comparison_is_case_insensitive_and_stripped(self) -> None:
        assert who_mgr(self._row("jim", owner_git=" CLAUDE ")) == "jim"

    def test_owner_git_equal_to_self_and_no_os_user_is_blank(self) -> None:
        assert who_mgr(self._row("", owner_git="claude")) == ""

    def test_both_equal_to_self_is_blank(self) -> None:
        assert who_mgr(self._row("claude", owner_git="claude")) == ""

    def test_owner_git_unset_uses_the_os_user_floor(self) -> None:
        assert who_mgr(self._row("jim")) == "jim"

    def test_full_name_is_not_addressable_and_falls_through(self) -> None:
        assert who_mgr(self._row("jim", owner_git="Jane Doe")) == "jim"
        assert who_mgr(self._row("", owner_git="Jane Doe")) == ""

    def test_rejected_candidate_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("DEBUG", logger="biff.formatting"):
            assert who_mgr(self._row("", owner_git="Jane Doe")) == ""
        assert "Jane Doe" in caplog.text

    def test_unaddressable_os_user_is_blank(self) -> None:
        assert who_mgr(self._row("a b")) == ""

    def test_human_row_is_always_blank(self) -> None:
        human = self._row("jim", user="kai", kind="human", owner_git="jmf-pobox")
        assert who_mgr(human) == ""
        assert who_mgr(self._row("jim", kind="", owner_git="jmf-pobox")) == ""

    def test_human_rows_keep_the_column_when_an_agent_has_one(self) -> None:
        lines = format_who(
            [self._row("jim", user="kai", kind="human"), self._row("jim")]
        ).splitlines()
        assert "MGR" in lines[0].split()
        assert "jim" not in lines[1].split()[1:]
        assert "jim" in lines[2].split()

    def test_column_hidden_when_no_row_has_a_manager(self) -> None:
        out = format_who([self._row(""), self._row("claude", owner_git="claude")])
        assert "MGR" not in out
        assert out.splitlines()[1].split() == [
            "claude:tty9",
            "[A]",
            "biff",
            "0m",
            "+",
            "-",
            "h",
        ]

    def test_labelled_name_round_trips_through_address_resolution(self) -> None:
        from biff.server.tools._session import resolve_tty_name
        from biff.tty import parse_address

        row = self._row("jim")
        name = format_who([row]).splitlines()[1].split()[0]
        user, tty = parse_address(name)
        assert tty is not None
        assert resolve_tty_name([row], user, tty) is row

    def test_row_from_before_the_fields_renders_as_without_them(self) -> None:
        row = self._row("")
        data = row.model_dump(mode="json")
        del data["launched_by"]
        del data["owner_git"]
        old = UserSession.model_validate(data)
        assert old.owner_git == ""
        assert format_who([old]) == format_who([row])

    def test_row_from_before_owner_git_falls_to_launched_by(self) -> None:
        data = self._row("jim").model_dump(mode="json")
        del data["owner_git"]
        assert who_mgr(UserSession.model_validate(data)) == "jim"

    def test_overlong_value_is_clipped(self) -> None:
        out = format_who([self._row("r" * 128)])
        assert "r" * 100 not in out
        assert "MGR" in out

    def test_value_beyond_the_identity_bound_is_not_addressable(self) -> None:
        assert who_mgr(self._row("r" * 129)) == ""

    def test_hostile_value_is_never_addressable_and_never_rendered(self) -> None:
        out = format_who([self._row("a\x1b[31mb\nc")])
        assert "\x1b" not in out
        assert len(out.splitlines()) == 2


def _git(repo: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],  # noqa: S607
        check=True,
        capture_output=True,
    )


class TestGitUserName:
    def _repo(self, tmp_path: Path, name: str | None) -> Path:
        repo = tmp_path / "r"
        repo.mkdir()
        _git(repo, "init", "-q")
        if name is not None:
            _git(repo, "config", "user.name", name)
        return repo

    def test_reads_the_config_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
        assert git_user_name(self._repo(tmp_path, "jmf-pobox")) == "jmf-pobox"

    def test_provisioned_host_config_wins_over_author_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
        monkeypatch.setenv("GIT_AUTHOR_NAME", "claude")
        monkeypatch.setenv("GIT_COMMITTER_NAME", "claude")
        repo = self._repo(tmp_path, "jmf-pobox")
        row = UserSession(
            user="claude",
            tty="a",
            kind="agent",
            launched_by="osjim",
            owner_git=git_user_name(repo),
        )
        assert who_mgr(row) == "jmf-pobox"

    @pytest.mark.parametrize(
        ("written", "want"),
        [
            ('\\"jmf-pobox\\"', "jmf-pobox"),
            ("'jmf-pobox'", "jmf-pobox"),
            ("jmf-pobox", "jmf-pobox"),
            ("'mismatch\\\"", "'mismatch\""),
        ],
    )
    def test_surrounding_quotes_are_stripped(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        written: str,
        want: str,
    ) -> None:
        # ``written`` is the config-file text; git's own quoting yields the
        # literal quotes that the reader must remove.
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
        repo = self._repo(tmp_path, None)
        (repo / ".git" / "config").write_text(f"[user]\n\tname = {written}\n")
        assert git_user_name(repo) == want

    def test_quoted_value_renders_unquoted_in_who(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
        repo = self._repo(tmp_path, None)
        (repo / ".git" / "config").write_text('[user]\n\tname = \\"jmf-pobox\\"\n')
        row = UserSession(
            user="claude", tty="a", kind="agent", owner_git=git_user_name(repo)
        )
        out = format_who([row])
        assert "jmf-pobox" in out
        assert '"' not in out

    def test_unset_is_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
        assert git_user_name(self._repo(tmp_path, None)) == ""

    def test_unset_is_logged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
        with caplog.at_level("DEBUG", logger="biff.identity"):
            git_user_name(self._repo(tmp_path, None))
        assert "user.name" in caplog.text

    def test_missing_git_is_empty_not_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", str(tmp_path))
        assert git_user_name(tmp_path) == ""


class TestFingerLaunchedBy:
    def _row(self, launched_by: str) -> UserSession:
        return UserSession(
            user="claude",
            tty="abcd1234",
            tty_name="tty9",
            kind="agent",
            launched_by=launched_by,
            last_active=datetime.now(UTC),
            last_tool_at=datetime.now(UTC),
        )

    def test_shown_without_ethos(self) -> None:
        out = format_finger(self._row("jim"))
        assert "   Launched by: jim\n" in out
        assert "Role" not in out

    def test_shown_with_ethos(self) -> None:
        roles = (RoleEntry("coo", "engineering", "ceo", "jfreeman"),)
        out = format_finger(self._row("jim"), roles)
        assert "   Launched by: jim\n" in out
        assert "   Role: coo" in out

    def test_absent_when_unknown(self) -> None:
        assert "Launched by" not in format_finger(self._row(""))

    def test_hostile_and_overlong_value_is_neutralised_and_clipped(self) -> None:
        out = format_finger(self._row("a\x1b[31m" + "z" * 300))
        assert "\x1b" not in out
        assert "z" * 100 not in out


class TestFingerRoles:
    def _row(self) -> UserSession:
        return UserSession(
            user="claude",
            tty="abcd1234",
            tty_name="tty9",
            kind="agent",
            last_active=datetime.now(UTC),
            last_tool_at=datetime.now(UTC),
        )

    def test_no_ethos_output_is_unchanged(self) -> None:
        row = self._row()
        out = format_finger(row)
        assert "Role" not in out
        assert out == format_finger(row, ())
        assert format_finger_multi([row]) == format_finger_multi([row], ())

    def test_single_role_line_between_header_and_on_since(self) -> None:
        roles = (RoleEntry("coo", "engineering", "ceo", "jfreeman"),)
        lines = format_finger(self._row(), roles).splitlines()
        role = next(i for i, ln in enumerate(lines) if "Role:" in ln)
        since = next(i for i, ln in enumerate(lines) if "On since" in ln)
        assert lines[0].startswith("\u25b6")
        assert 0 < role < since
        assert lines[role] == (
            "   Role: coo (engineering) \u2014 reports to ceo (jfreeman)"
        )
        assert "Roles:" not in "\n".join(lines)

    def test_multi_role_block_one_line_per_role(self) -> None:
        roles = (
            RoleEntry("coo", "engineering", "ceo", "jfreeman"),
            RoleEntry("reviewer", "gstack", "coo", "claude"),
        )
        out = format_finger_multi([self._row()], roles)
        assert "   Roles:\n" in out
        assert "     coo (engineering) \u2014 reports to ceo (jfreeman)\n" in out
        assert "     reviewer (gstack) \u2014 reports to coo (claude)\n" in out
        assert out.index("Roles:") < out.index("On since")

    def test_role_without_manager_has_no_reports_clause(self) -> None:
        out = format_finger(self._row(), (RoleEntry("ceo", "engineering"),))
        assert "   Role: ceo (engineering)\n" in out
        assert "reports to" not in out

    def test_hostile_and_overlong_role_text_is_neutralised_and_clipped(self) -> None:
        roles = (RoleEntry("a\x1b[31mb\nc", "t" * 300, "m" * 300, "p"),)
        out = format_finger(self._row(), roles)
        assert "\x1b" not in out
        assert "t" * 100 not in out
        assert len([ln for ln in out.splitlines() if "Role:" in ln]) == 1
