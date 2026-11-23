"""The Claude Code adapter satisfies the contract and does what enable did before."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

from biff._cli_runtime import CliInvocation
from biff.plugins.claude_code.claude_permissions import (
    BIFF_RULES,
    ProjectPermissions,
)
from biff.plugins.claude_code.installer import ClaudeCodeInstaller
from biff.plugins.contract import Installer

if TYPE_CHECKING:
    from pathlib import Path

_ALL_RULES = (*BIFF_RULES["allow"], *BIFF_RULES["ask"])
_IMPORT = "@.punt-labs/biff/CLAUDE.md"


def test_real_adapter_satisfies_installer() -> None:
    installer: Installer = ClaudeCodeInstaller()
    assert installer.check()[0].name == "Status line"


def test_cli_registers_the_claude_code_installer() -> None:
    installers = CliInvocation.plugin_registry().installers()
    assert [type(i) for i in installers] == [ClaudeCodeInstaller]


class TestGrantAndRevoke:
    def test_grant_adds_every_rule_and_names_the_file(self, tmp_path: Path) -> None:
        change = ClaudeCodeInstaller().grant(tmp_path)
        assert change.changed == _ALL_RULES

    def test_grant_report_lines_match_direct_use(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        direct = ProjectPermissions(tmp_path / "a").grant()
        via_installer = ClaudeCodeInstaller().grant(tmp_path / "b")
        assert via_installer.lines("added to") == direct.lines("added to")
        assert via_installer.lines("added to")[0] == (
            "Permission added to .claude/settings.json: Bash(biff:*)"
        )

    def test_refused_grant_carries_the_fix(self, tmp_path: Path) -> None:
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "settings.json").write_text("not json{{")
        change = ClaudeCodeInstaller().grant(tmp_path)
        assert (change.error, change.fix) == (change.error, "Fix .claude/settings.json")
        assert change.error

    def test_revoke_removes_what_grant_added(self, tmp_path: Path) -> None:
        installer = ClaudeCodeInstaller()
        installer.grant(tmp_path)
        assert installer.revoke(tmp_path).changed == _ALL_RULES

    def test_revoke_of_selected_rules_leaves_the_rest(self, tmp_path: Path) -> None:
        installer = ClaudeCodeInstaller()
        installer.grant(tmp_path)
        installer.revoke(tmp_path, ["Bash(biff:*)"])
        allowed = json.loads((tmp_path / ".claude" / "settings.json").read_text())
        assert "Bash(biff:*)" not in allowed["permissions"]["allow"]
        assert "Skill(biff:*)" in allowed["permissions"]["allow"]


class TestGuideImport:
    def test_register_appends_the_import_once(self, tmp_path: Path) -> None:
        installer = ClaudeCodeInstaller()
        (tmp_path / "CLAUDE.md").write_text("# project\n")
        assert installer.register_guide(tmp_path) is True
        assert installer.register_guide(tmp_path) is False
        assert (tmp_path / "CLAUDE.md").read_text().count(_IMPORT) == 1

    def test_unregister_removes_it(self, tmp_path: Path) -> None:
        installer = ClaudeCodeInstaller()
        (tmp_path / "CLAUDE.md").write_text("# project\n")
        installer.register_guide(tmp_path)
        assert installer.unregister_guide(tmp_path) is True
        assert _IMPORT not in (tmp_path / "CLAUDE.md").read_text()

    def test_preflight_refuses_an_unclosed_fence(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("```text\nnever closed\n")
        change = ClaudeCodeInstaller().preflight(tmp_path)
        assert "unclosed code fence" in change.error
        assert change.fix == "Close the code fence in CLAUDE.md"

    def test_preflight_passes_a_clean_file(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("# project\n")
        assert not ClaudeCodeInstaller().preflight(tmp_path).error


class TestStatuslineCheck:
    def _check(self, settings: Path) -> tuple[bool, bool, str]:
        with patch("biff.plugins.claude_code.installer.SETTINGS_PATH", settings):
            (result,) = ClaudeCodeInstaller().check()
        return result.passed, result.required, result.message

    def test_configured(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(
            json.dumps(
                {"statusLine": {"type": "command", "command": "/p/biff statusline"}}
            )
        )
        assert self._check(settings) == (True, False, "configured")

    def test_not_configured(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({}))
        passed, required, _ = self._check(settings)
        assert (passed, required) == (False, False)

    def test_corrupt_settings(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text("not valid json{{{")
        assert self._check(settings) == (
            False,
            False,
            "could not read settings.json",
        )

    def test_configured_for_another_tool(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(
            json.dumps(
                {"statusLine": {"type": "command", "command": "other-tool status"}}
            )
        )
        passed, required, _ = self._check(settings)
        assert (passed, required) == (False, False)
