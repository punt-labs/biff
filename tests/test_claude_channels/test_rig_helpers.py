"""The tier's own selection and stray-write checks, which need no Claude Code."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from tests.test_claude_channels.launches import DEFAULT_LAUNCH, select_launches
from tests.test_claude_channels.rig import find_strays


class TestSelectLaunches:
    def test_unset_selects_the_verified_launch(self) -> None:
        assert select_launches(None) == [DEFAULT_LAUNCH]

    def test_names_are_split_and_trimmed(self) -> None:
        assert select_launches("inline, server-tty") == ["inline", "server-tty"]

    @pytest.mark.parametrize("raw", ["", "  ", ",", " , "])
    def test_blank_value_raises(self, raw: str) -> None:
        with pytest.raises(ValueError, match="blank"):
            select_launches(raw)

    def test_unknown_name_raises(self) -> None:
        with pytest.raises(ValueError, match="nonesuch"):
            select_launches("inline,nonesuch")


class TestFindStrays:
    def test_file_naming_a_marker_is_a_stray(self, tmp_path: Path) -> None:
        (tmp_path / "active").mkdir()
        leak = tmp_path / "active" / "handle"
        leak.write_text("repo=/tmp/biff-channels-abc/repo")

        assert find_strays(tmp_path, 0.0, ("/tmp/biff-channels-abc",)) == [leak]

    def test_file_named_for_a_marker_is_a_stray(self, tmp_path: Path) -> None:
        leak = tmp_path / "alice123-session"
        leak.write_text("x")

        assert find_strays(tmp_path, 0.0, ("alice123",)) == [leak]

    def test_unrelated_file_is_not(self, tmp_path: Path) -> None:
        (tmp_path / "other").write_text("someone else's session")

        assert find_strays(tmp_path, 0.0, ("alice123",)) == []

    def test_file_older_than_the_rig_is_not(self, tmp_path: Path) -> None:
        old = tmp_path / "old"
        old.write_text("alice123")
        os.utime(old, (1.0, 1.0))

        assert find_strays(tmp_path, time.time() - 10, ("alice123",)) == []

    def test_missing_home_has_no_strays(self, tmp_path: Path) -> None:
        assert find_strays(tmp_path / "absent", 0.0, ("alice123",)) == []
