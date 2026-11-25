"""The tier's own selection and stray-write checks, which need no Claude Code."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_claude_channels.launches import (
    DEFAULT_LAUNCH,
    LAUNCHES,
    select_launches,
)
from tests.test_claude_channels.rig import (
    REPO_ROOT,
    ChannelRig,
    Launch,
    channel_rig,
    find_strays,
)


def _pytest(*args: str, selection: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "BIFF_CHANNEL_LAUNCHES": selection}
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


class TestInvalidSelection:
    @pytest.mark.parametrize("selection", ["", "nonesuch"])
    def test_default_collection_still_succeeds(self, selection: str) -> None:
        done = _pytest(
            "--collect-only", "-q", "tests/test_claude_channels", selection=selection
        )

        assert done.returncode == 0, done.stdout + done.stderr

    def test_running_the_tier_with_a_blank_selection_fails(self) -> None:
        done = _pytest(
            "-m", "claude_channels", "tests/test_claude_channels", selection=""
        )

        assert done.returncode != 0
        assert "BIFF_CHANNEL_LAUNCHES is blank" in done.stdout


class TestChannelRigCleanup:
    def test_strays_are_noted_on_a_setup_failure_and_the_root_is_removed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stray = Path("/real/biff/home/handle")

        def fake_strays(_self: ChannelRig) -> list[Path]:
            return [stray]

        monkeypatch.setattr(ChannelRig, "strays", fake_strays)
        roots: list[Path] = []
        real_init = ChannelRig.__init__

        def remember(self: ChannelRig, root: Path, launch: Launch, url: str) -> None:
            roots.append(root)
            real_init(self, root, launch, url)

        monkeypatch.setattr(ChannelRig, "__init__", remember)

        with (
            pytest.raises(RuntimeError, match="no handshake") as caught,
            channel_rig(LAUNCHES["inline"], "nats://127.0.0.1:1"),
        ):
            msg = "no handshake"
            raise RuntimeError(msg)

        assert any(str(stray) in note for note in caught.value.__notes__)
        assert not roots[0].exists()


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
