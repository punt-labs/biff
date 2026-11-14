"""Tests for ``ProjectPermissions``: biff's allow rules, repo-level only."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest

from biff import claude_permissions
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
        assert lines == [
            f"Permission added to .claude/settings.json: {rule}" for rule in BIFF_RULES
        ]

    def test_no_blanket_cli_wildcard(self) -> None:
        assert "Bash(biff:*)" not in BIFF_RULES
        for verb in ("install", "uninstall", "install-statusline", "enable", "disable"):
            assert not any(rule.startswith(f"Bash(biff {verb}:") for rule in BIFF_RULES)
        assert "Bash(biff who:*)" in BIFF_RULES


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

    def test_revoke_subset_leaves_other_rules(self, tmp_path: Path) -> None:
        _write(tmp_path, {"permissions": {"allow": list(BIFF_RULES)}})
        change = ProjectPermissions(tmp_path).revoke(BIFF_RULES[:2])
        assert change.changed == BIFF_RULES[:2]
        assert _allow(tmp_path) == list(BIFF_RULES[2:])

    def test_revoke_accepts_an_iterator(self, tmp_path: Path) -> None:
        _write(tmp_path, {"permissions": {"allow": list(BIFF_RULES)}})
        change = ProjectPermissions(tmp_path).revoke(iter(BIFF_RULES[:3]))
        assert change.changed == BIFF_RULES[:3]
        assert _allow(tmp_path) == list(BIFF_RULES[3:])

    def test_revoke_nothing_writes_nothing(self, tmp_path: Path) -> None:
        assert ProjectPermissions(tmp_path).revoke(()) == PermissionChange()
        assert not (tmp_path / ".claude").exists()


class TestHardening:
    def test_refuses_invalid_utf8(self, tmp_path: Path) -> None:
        _settings(tmp_path).parent.mkdir()
        _settings(tmp_path).write_bytes(b"\xff\xfe{}")
        change = ProjectPermissions(tmp_path).grant()
        assert "not valid UTF-8" in change.error

    def test_does_not_follow_symlinked_lock(self, tmp_path: Path) -> None:
        victim = tmp_path / "victim.txt"
        victim.write_text("keep me")
        (tmp_path / ".claude").mkdir()
        lock = tmp_path / ".claude" / ".settings.json.punt-import.lock"
        lock.symlink_to(victim)
        change = ProjectPermissions(tmp_path).grant()
        assert change.error
        assert victim.read_text() == "keep me"

    def test_refuses_non_regular_settings(self, tmp_path: Path) -> None:
        (tmp_path / ".claude").mkdir()
        os.mkfifo(_settings(tmp_path))
        change = ProjectPermissions(tmp_path).grant()
        assert "not a regular file" in change.error

    def test_refusal_creates_no_lock(self, tmp_path: Path) -> None:
        (tmp_path / ".claude").mkdir()
        _settings(tmp_path).write_text("{not json")
        assert ProjectPermissions(tmp_path).grant().error
        assert sorted(p.name for p in (tmp_path / ".claude").iterdir()) == [
            "settings.json"
        ]

    def test_lock_is_not_truncated(self, tmp_path: Path) -> None:
        (tmp_path / ".claude").mkdir()
        lock = tmp_path / ".claude" / ".settings.json.punt-import.lock"
        lock.write_text("held by another tool")
        assert not ProjectPermissions(tmp_path).grant().error
        assert lock.read_text() == "held by another tool"

    def test_swapped_claude_dir_cannot_redirect_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``.claude`` swapped for a symlink after it is opened: the write stays put.

        Every step is relative to the pinned directory descriptor, so the
        settings land in the directory that was opened, never the link target.
        """
        repo = tmp_path / "repo"
        (repo / ".claude").mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        moved = tmp_path / "moved"
        real_read = claude_permissions._read

        def swap_then_read(dir_fd: int) -> tuple[dict[str, object], int]:
            if not moved.exists():
                (repo / ".claude").rename(moved)
                (repo / ".claude").symlink_to(outside, target_is_directory=True)
            return real_read(dir_fd)

        monkeypatch.setattr(claude_permissions, "_read", swap_then_read)
        change = ProjectPermissions(repo).grant()
        assert change.changed == BIFF_RULES
        assert list(outside.iterdir()) == []
        allow = json.loads((moved / "settings.json").read_text())["permissions"]
        assert allow["allow"] == list(BIFF_RULES)
