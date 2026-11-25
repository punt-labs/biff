"""ClaudeLaunch: the restart advice names an entry the running plugin matches."""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.plugins.claude_code.claude_launch import ClaudeLaunch

if TYPE_CHECKING:
    from pathlib import Path

DEV = "--dangerously-load-development-channels"


def test_a_marketplace_session_is_told_the_punt_labs_entry() -> None:
    advice = ClaudeLaunch(["claude"], {}).restart_advice()
    assert advice.startswith(f"Restart Claude Code with {DEV} plugin:biff@punt-labs.")
    assert "--channels plugin:biff@punt-labs" in advice


def test_an_unreadable_manifest_falls_back_to_the_dev_name(tmp_path: Path) -> None:
    env = {"CLAUDE_PLUGIN_ROOT": str(tmp_path / "missing")}
    advice = ClaudeLaunch(["claude", "--plugin-dir", "x"], env).restart_advice()
    assert advice == f"Restart Claude Code with {DEV} plugin:biff-dev@inline."


def test_a_manifest_without_a_name_falls_back_too(tmp_path: Path) -> None:
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text("[]")
    env = {"CLAUDE_PLUGIN_ROOT": str(tmp_path)}
    advice = ClaudeLaunch(["claude", "--plugin-dir=x"], env).restart_advice()
    assert advice.endswith("plugin:biff-dev@inline.")


def test_a_flag_that_only_starts_like_plugin_dir_is_not_one() -> None:
    advice = ClaudeLaunch(["claude", "--plugin-directory", "x"], {}).restart_advice()
    assert "@punt-labs" in advice
