"""Effective managed policy for channels: sources, ranking and the gate (DES-087)."""

from __future__ import annotations

import json
import os
import plistlib
from pathlib import Path

import pytest

from biff.plugins.claude_code.managed_policy import ManagedPolicy, ManagedPolicyGate
from biff.plugins.claude_code.managed_sources import ManagedSources

BIFF_ENTRY = {"marketplace": "punt-labs", "plugin": "biff"}


def _write(directory: Path, name: str, body: object) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body if isinstance(body, str) else json.dumps(body))
    return path


def _policy(directory: Path) -> ManagedPolicy:
    source = ManagedSources.directory(directory)
    return ManagedPolicy(() if source is None else (source,))


def _gate_reasons(policy: ManagedPolicy) -> list[str]:
    return [gate.reason for gate in ManagedPolicyGate(policy).closed()]


class TestDirectorySource:
    """managed-settings.json and its drop-in directory."""

    def test_absent_directory_is_no_source(self, tmp_path: Path) -> None:
        assert ManagedSources.directory(tmp_path / "absent") is None

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
    def test_unreadable_file_is_no_source_and_logged_at_info(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = _write(tmp_path, "managed-settings.json", {"channelsEnabled": False})
        path.chmod(0)
        try:
            with caplog.at_level("DEBUG"):
                assert ManagedSources.directory(tmp_path) is None
        finally:
            path.chmod(0o600)
        assert [r.levelname for r in caplog.records] == ["INFO"]

    def test_malformed_file_is_skipped_without_a_warning(self, tmp_path: Path) -> None:
        _write(tmp_path, "managed-settings.json", "{not json")
        assert ManagedSources.directory(tmp_path) is None

    def test_drop_in_overrides_scalars_and_concatenates_arrays(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "managed-settings.json",
            {"channelsEnabled": False, "allowedChannelPlugins": ["a@b"]},
        )
        drop_in = tmp_path / "managed-settings.d"
        _write(drop_in, "10.json", {"channelsEnabled": True})
        _write(drop_in, "20.json", {"allowedChannelPlugins": ["biff@punt-labs"]})
        _write(drop_in, ".30-hidden.json", {"channelsEnabled": False})
        policy = _policy(tmp_path)
        assert policy.channels_enabled is True
        assert policy.lists_biff

    @pytest.mark.parametrize(
        ("platform", "expected"),
        [
            ("darwin", "/Library/Application Support/ClaudeCode"),
            ("linux", "/etc/claude-code"),
            ("win32", "C:\\Program Files\\ClaudeCode"),
            ("freebsd14", None),
        ],
    )
    def test_directory_per_platform(self, platform: str, expected: str | None) -> None:
        found = ManagedSources.directory_for(platform)
        assert (None if found is None else str(found)) == expected


class TestPlistSource:
    """The macOS MDM preference domain."""

    def test_reads_a_plist(self, tmp_path: Path) -> None:
        path = tmp_path / "com.anthropic.claudecode.plist"
        path.write_bytes(plistlib.dumps({"channelsEnabled": True}))
        assert ManagedSources.plist(path) == {"channelsEnabled": True}

    def test_absent_or_malformed_plist_is_no_source(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.plist"
        bad.write_bytes(b"not a plist")
        assert ManagedSources.plist(tmp_path / "absent.plist") is None
        assert ManagedSources.plist(bad) is None

    def test_macos_domain_path(self) -> None:
        assert str(ManagedSources.PLIST_MACOS) == (
            "/Library/Managed Preferences/com.anthropic.claudecode.plist"
        )


class TestRanking:
    """First source with a policy key wins; managedSourcesBehavior merges."""

    def test_higher_source_beats_lower(self) -> None:
        policy = ManagedPolicy(({"channelsEnabled": False}, {"channelsEnabled": True}))
        assert policy.channels_enabled is False

    def test_source_with_only_control_keys_is_ignored(self) -> None:
        policy = ManagedPolicy(
            ({"managedSourcesBehavior": "merge"}, {"channelsEnabled": True})
        )
        assert policy.channels_enabled is True

    def test_merge_behavior_combines_sources(self) -> None:
        policy = ManagedPolicy(
            (
                {
                    "managedSourcesBehavior": "merge",
                    "allowedChannelPlugins": ["biff@punt-labs"],
                },
                {"channelsEnabled": True, "allowedChannelPlugins": ["a@b"]},
            )
        )
        assert policy.channels_enabled is True
        assert policy.lists_biff

    def test_without_merge_the_lower_source_is_ignored(self) -> None:
        policy = ManagedPolicy(
            (
                {"allowedChannelPlugins": []},
                {"allowedChannelPlugins": ["biff@punt-labs"]},
            )
        )
        assert not policy.lists_biff

    @pytest.mark.parametrize(
        "entry", [BIFF_ENTRY, "biff@punt-labs"], ids=["object", "string"]
    )
    def test_entry_shapes(self, entry: object) -> None:
        assert ManagedPolicy(({"allowedChannelPlugins": [entry]},)).lists_biff

    @pytest.mark.parametrize(
        "listed",
        [[], ["biff@other"], [{"plugin": "biff"}], "biff@punt-labs", None],
    )
    def test_biff_not_listed(self, listed: object) -> None:
        source: dict[str, object] = {}
        if listed is not None:
            source["allowedChannelPlugins"] = listed
        assert not ManagedPolicy((source,)).lists_biff


class TestGate:
    """channelsEnabled is a finding only when set to a non-true value."""

    def test_no_sources_no_finding(self) -> None:
        assert _gate_reasons(ManagedPolicy(())) == []

    def test_unrelated_file_no_finding(self, tmp_path: Path) -> None:
        _write(tmp_path, "managed-settings.json", {"permissions": {"allow": []}})
        assert _gate_reasons(_policy(tmp_path)) == []

    def test_control_keys_only_no_finding(self, tmp_path: Path) -> None:
        _write(tmp_path, "managed-settings.json", {"managedSourcesBehavior": "merge"})
        assert _gate_reasons(_policy(tmp_path)) == []

    def test_true_no_finding(self) -> None:
        assert _gate_reasons(ManagedPolicy(({"channelsEnabled": True},))) == []

    def test_string_true_is_accepted_and_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("INFO"):
            assert _gate_reasons(ManagedPolicy(({"channelsEnabled": "true"},))) == []
        assert "channelsEnabled" in caplog.text

    @pytest.mark.parametrize("value", [False, "false", 0])
    def test_non_true_value_is_a_finding(self, value: object) -> None:
        gates = ManagedPolicyGate(ManagedPolicy(({"channelsEnabled": value},))).closed()
        assert len(gates) == 1
        assert "channelsEnabled" in gates[0].reason
        assert "set channelsEnabled to true" in gates[0].fix
