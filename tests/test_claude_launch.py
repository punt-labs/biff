"""ClaudeLaunch: the restart advice names an entry the running plugin matches."""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.plugins.claude_code.claude_launch import ClaudeLaunch

if TYPE_CHECKING:
    from pathlib import Path

DEV = "--dangerously-load-development-channels"


def _plugin(root: Path, manifest: str = '{"name": "biff-dev"}') -> dict[str, str]:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(manifest)
    return {"CLAUDE_PLUGIN_ROOT": str(root)}


def test_a_marketplace_session_is_told_the_punt_labs_entry() -> None:
    advice = ClaudeLaunch(["claude"], {}).restart_advice()
    assert advice.startswith(f"Restart Claude Code with {DEV} plugin:biff@punt-labs.")
    assert "--channels plugin:biff@punt-labs" in advice


def test_this_plugins_own_dir_gets_the_inline_entry(tmp_path: Path) -> None:
    env = _plugin(tmp_path)
    advice = ClaudeLaunch(["claude", "--plugin-dir", str(tmp_path)], env)
    assert (
        advice.restart_advice()
        == f"Restart Claude Code with {DEV} plugin:biff-dev@inline."
    )


def test_the_equals_form_names_this_plugin_too(tmp_path: Path) -> None:
    env = _plugin(tmp_path)
    advice = ClaudeLaunch(["claude", f"--plugin-dir={tmp_path}"], env).restart_advice()
    assert advice.endswith("plugin:biff-dev@inline.")


def test_another_plugins_dir_leaves_marketplace_biff_alone(tmp_path: Path) -> None:
    """An inline Quarry must not make marketplace biff look inline."""
    env = _plugin(tmp_path / "biff")
    quarry = tmp_path / "quarry"
    quarry.mkdir()
    advice = ClaudeLaunch(["claude", "--plugin-dir", str(quarry)], env).restart_advice()
    assert "plugin:biff@punt-labs" in advice
    assert "@inline" not in advice


def test_a_released_inline_plugin_is_named_biff(tmp_path: Path) -> None:
    env = _plugin(tmp_path, '{"name": "biff"}')
    advice = ClaudeLaunch(["claude", "--plugin-dir", str(tmp_path)], env)
    assert advice.restart_advice().endswith("plugin:biff@inline.")


def test_a_manifest_without_a_string_name_falls_back_to_dev(tmp_path: Path) -> None:
    env = _plugin(tmp_path, '{"name": 7}')
    advice = ClaudeLaunch(["claude", "--plugin-dir", str(tmp_path)], env)
    assert advice.restart_advice().endswith("plugin:biff-dev@inline.")


def test_a_flag_that_only_starts_like_plugin_dir_is_not_one(tmp_path: Path) -> None:
    env = _plugin(tmp_path)
    advice = ClaudeLaunch(["claude", "--plugin-directory", str(tmp_path)], env)
    assert "@punt-labs" in advice.restart_advice()
