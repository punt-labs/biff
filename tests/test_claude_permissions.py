"""Tests for ``ProjectPermissions``: biff's allow rules, repo-level only."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from biff.claude_permissions import BIFF_RULES, PermissionChange, ProjectPermissions

if TYPE_CHECKING:
    from pathlib import Path


def _settings(root: Path) -> Path:
    return root / ".claude" / "settings.json"


def _write(root: Path, data: object) -> None:
    path = _settings(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _allow(root: Path) -> list[str]:
    data = json.loads(_settings(root).read_text())
    allow: list[str] = data["permissions"]["allow"]
    return allow


class TestGrant:
    def test_creates_settings_with_every_rule(self, tmp_path: Path) -> None:
        change = ProjectPermissions(tmp_path).grant()
        assert change == PermissionChange(changed=BIFF_RULES)
        assert _allow(tmp_path) == list(BIFF_RULES)

    def test_keeps_other_settings_and_rules(self, tmp_path: Path) -> None:
        _write(tmp_path, {"env": {"A": "1"}, "permissions": {"allow": ["Read"]}})
        ProjectPermissions(tmp_path).grant()
        data = json.loads(_settings(tmp_path).read_text())
        assert data["env"] == {"A": "1"}
        assert data["permissions"]["allow"] == ["Read", *BIFF_RULES]

    def test_adds_only_missing_rules(self, tmp_path: Path) -> None:
        _write(tmp_path, {"permissions": {"allow": [BIFF_RULES[0]]}})
        change = ProjectPermissions(tmp_path).grant()
        assert change.changed == BIFF_RULES[1:]

    def test_noop_does_not_rewrite(self, tmp_path: Path) -> None:
        ProjectPermissions(tmp_path).grant()
        before = _settings(tmp_path).stat().st_mtime_ns
        assert ProjectPermissions(tmp_path).grant() == PermissionChange()
        assert _settings(tmp_path).stat().st_mtime_ns == before

    def test_never_grants_dev_rules(self, tmp_path: Path) -> None:
        ProjectPermissions(tmp_path).grant()
        assert not [rule for rule in _allow(tmp_path) if "biff-dev" in rule]

    @pytest.mark.parametrize(
        ("content", "reason"),
        [
            ("{not json", "not valid JSON"),
            ("[]", "not a JSON object"),
            ('{"permissions": []}', "'permissions' is not an object"),
            ('{"permissions": {"allow": "x"}}', "'permissions.allow' is not a list"),
        ],
    )
    def test_refuses_malformed_settings(
        self, tmp_path: Path, content: str, reason: str
    ) -> None:
        _settings(tmp_path).parent.mkdir()
        _settings(tmp_path).write_text(content)
        change = ProjectPermissions(tmp_path).grant()
        assert reason in change.error
        assert _settings(tmp_path).read_text() == content

    def test_refuses_symlinked_settings_file(self, tmp_path: Path) -> None:
        target = tmp_path / "elsewhere.json"
        target.write_text("{}")
        (tmp_path / ".claude").mkdir()
        _settings(tmp_path).symlink_to(target)
        change = ProjectPermissions(tmp_path).grant()
        assert "symlink" in change.error
        assert target.read_text() == "{}"

    def test_refuses_symlinked_claude_dir(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".claude").symlink_to(outside)
        change = ProjectPermissions(repo).grant()
        assert "symlink" in change.error
        assert list(outside.iterdir()) == []

    def test_refuses_home_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        change = ProjectPermissions(tmp_path).grant()
        assert "home directory" in change.error
        assert not (tmp_path / ".claude").exists()

    def test_lines_report_each_rule(self, tmp_path: Path) -> None:
        lines = ProjectPermissions(tmp_path).grant().lines("added to")
        assert len(lines) == 1
        assert all(rule in lines[0] for rule in BIFF_RULES)


class TestRevoke:
    def test_removes_exactly_biff_rules(self, tmp_path: Path) -> None:
        _write(tmp_path, {"permissions": {"allow": ["Read", *BIFF_RULES, "Bash(x)"]}})
        change = ProjectPermissions(tmp_path).revoke()
        assert change.changed == BIFF_RULES
        assert _allow(tmp_path) == ["Read", "Bash(x)"]

    def test_absent_settings_creates_nothing(self, tmp_path: Path) -> None:
        assert ProjectPermissions(tmp_path).revoke() == PermissionChange()
        assert not (tmp_path / ".claude").exists()

    def test_lines_empty_when_unchanged(self, tmp_path: Path) -> None:
        assert ProjectPermissions(tmp_path).revoke().lines("removed from") == []
