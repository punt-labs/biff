"""Start-up warning that biff channels cannot be live (DES-087)."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from biff.plugins.claude_code._hook_entry import (
    _cc_session_resume,
    _cc_session_start,
)
from biff.plugins.claude_code.channel_gates import ClosedGate, FeatureFlagGate
from biff.plugins.claude_code.managed_policy import ManagedPolicyGate
from biff.plugins.claude_code.startup_warning import StartupWarning

FLAGS = (
    "DISABLE_TELEMETRY",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "DO_NOT_TRACK",
    "DISABLE_GROWTHBOOK",
)
POLL = "Run /biff:read 2m to poll."
BIFF_ENTRY = {"marketplace": "punt-labs", "plugin": "biff"}


def _write(directory: Path, name: str, body: object) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body if isinstance(body, str) else json.dumps(body))
    return path


def _warning(env: dict[str, str], managed_dir: Path | None) -> str | None:
    return StartupWarning(
        (*FeatureFlagGate.all_in(env), ManagedPolicyGate(managed_dir))
    ).text()


class TestFeatureFlags:
    """A feature-flag opt-out closes the gate Claude Code needs for channels."""

    @pytest.mark.parametrize("flag", FLAGS)
    def test_each_flag_warns_by_name(self, flag: str, tmp_path: Path) -> None:
        text = _warning({flag: "1"}, tmp_path / "absent")
        assert text is not None
        assert text.startswith("biff: channels are not live in this session (")
        assert f"{flag} is set" in text
        assert "New messages will not arrive on their own." in text
        assert POLL in text
        assert f"Unset {flag} and restart Claude Code." in text

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_values_close_the_gate(self, value: str, tmp_path: Path) -> None:
        assert _warning({"DO_NOT_TRACK": value}, tmp_path) is not None

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
    def test_falsy_values_leave_the_gate_open(self, value: str, tmp_path: Path) -> None:
        assert _warning({"DO_NOT_TRACK": value}, tmp_path) is None

    def test_two_flags_name_both(self, tmp_path: Path) -> None:
        text = _warning({"DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"}, tmp_path)
        assert text is not None
        assert "DISABLE_TELEMETRY is set" in text
        assert "DO_NOT_TRACK is set" in text
        assert text.count(POLL) == 1


class TestManagedPolicy:
    """Managed settings must allow channels and list biff."""

    def test_no_directory_is_no_restriction(self, tmp_path: Path) -> None:
        assert _warning({}, tmp_path / "absent") is None

    def test_no_known_directory_is_no_restriction(self) -> None:
        assert _warning({}, None) is None

    def test_empty_directory_is_no_restriction(self, tmp_path: Path) -> None:
        assert _warning({}, tmp_path) is None

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
    def test_unreadable_file_is_no_restriction(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "managed-settings.json", {"channelsEnabled": False})
        path.chmod(0)
        try:
            assert _warning({}, tmp_path) is None
        finally:
            path.chmod(0o600)

    def test_malformed_json_names_the_file(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "managed-settings.json", "{not json")
        text = _warning({}, tmp_path)
        assert text is not None
        assert f"{path} is not valid JSON" in text
        assert POLL in text

    def test_non_object_json_is_malformed(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "managed-settings.json", "[1]")
        text = _warning({}, tmp_path)
        assert text is not None
        assert f"{path} is not valid JSON" in text

    @pytest.mark.parametrize("body", [{}, {"channelsEnabled": False}])
    def test_channels_not_enabled(
        self, body: dict[str, object], tmp_path: Path
    ) -> None:
        _write(tmp_path, "managed-settings.json", body)
        text = _warning({}, tmp_path)
        assert text is not None
        assert f"managed settings in {tmp_path} do not set channelsEnabled" in text
        assert "Ask your administrator to set channelsEnabled to true" in text

    @pytest.mark.parametrize(
        "allowed",
        [
            [BIFF_ENTRY],
            ["biff@punt-labs"],
            [{"marketplace": "claude-plugins-official", "plugin": "tg"}, BIFF_ENTRY],
        ],
    )
    def test_enabled_and_listed_is_open(
        self, allowed: list[object], tmp_path: Path
    ) -> None:
        body = {"channelsEnabled": True, "allowedChannelPlugins": allowed}
        _write(tmp_path, "managed-settings.json", body)
        assert _warning({}, tmp_path) is None

    @pytest.mark.parametrize(
        "body",
        [
            {"channelsEnabled": True},
            {"channelsEnabled": True, "allowedChannelPlugins": []},
            {
                "channelsEnabled": True,
                "allowedChannelPlugins": [
                    {"marketplace": "claude-plugins-official", "plugin": "telegram"}
                ],
            },
            {"channelsEnabled": True, "allowedChannelPlugins": ["biff@other"]},
            {"channelsEnabled": True, "allowedChannelPlugins": "biff@punt-labs"},
        ],
    )
    def test_biff_not_listed(self, body: dict[str, object], tmp_path: Path) -> None:
        _write(tmp_path, "managed-settings.json", body)
        text = _warning({}, tmp_path)
        assert text is not None
        assert f"managed settings in {tmp_path} do not list biff" in text
        assert "allowedChannelPlugins" in text
        assert "punt-labs" in text

    def test_drop_in_merges_over_the_base(self, tmp_path: Path) -> None:
        _write(tmp_path, "managed-settings.json", {"channelsEnabled": True})
        drop_in = tmp_path / "managed-settings.d"
        _write(drop_in, "20-channels.json", {"allowedChannelPlugins": [BIFF_ENTRY]})
        assert _warning({}, tmp_path) is None

    def test_later_drop_in_overrides_scalars(self, tmp_path: Path) -> None:
        _write(tmp_path, "managed-settings.json", {"channelsEnabled": True})
        drop_in = tmp_path / "managed-settings.d"
        _write(drop_in, "10-a.json", {"allowedChannelPlugins": [BIFF_ENTRY]})
        _write(drop_in, "20-b.json", {"channelsEnabled": False})
        text = _warning({}, tmp_path)
        assert text is not None
        assert "channelsEnabled" in text

    def test_drop_in_arrays_concatenate(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "managed-settings.json",
            {"channelsEnabled": True, "allowedChannelPlugins": ["a@b"]},
        )
        drop_in = tmp_path / "managed-settings.d"
        _write(drop_in, "10.json", {"allowedChannelPlugins": ["biff@punt-labs"]})
        assert _warning({}, tmp_path) is None

    def test_hidden_drop_ins_are_ignored(self, tmp_path: Path) -> None:
        _write(tmp_path, "managed-settings.json", {"channelsEnabled": True})
        drop_in = tmp_path / "managed-settings.d"
        _write(drop_in, ".10-hidden.json", {"allowedChannelPlugins": [BIFF_ENTRY]})
        assert _warning({}, tmp_path) is not None

    def test_malformed_drop_in_names_it_and_keeps_the_rest(
        self, tmp_path: Path
    ) -> None:
        body = {"channelsEnabled": True, "allowedChannelPlugins": [BIFF_ENTRY]}
        _write(tmp_path, "managed-settings.json", body)
        bad = _write(tmp_path / "managed-settings.d", "10-bad.json", "oops")
        text = _warning({}, tmp_path)
        assert text is not None
        assert f"{bad} is not valid JSON" in text
        assert "channelsEnabled" not in text

    @pytest.mark.parametrize(
        ("platform", "expected"),
        [
            ("darwin", Path("/Library/Application Support/ClaudeCode")),
            ("linux", Path("/etc/claude-code")),
            ("win32", None),
        ],
    )
    def test_directory_per_platform(self, platform: str, expected: Path | None) -> None:
        assert ManagedPolicyGate.directory_for(platform) == expected


class _BrokenGate:
    def closed(self) -> tuple[ClosedGate, ...]:
        msg = "boom"
        raise RuntimeError(msg)


class TestStartupWarning:
    """The assembled warning."""

    def test_a_failing_gate_is_logged_and_skipped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        gates = (_BrokenGate(), FeatureFlagGate("DO_NOT_TRACK", {"DO_NOT_TRACK": "1"}))
        text = StartupWarning(gates).text()
        assert text is not None
        assert "DO_NOT_TRACK is set" in text
        assert "_BrokenGate" in caplog.text

    def test_all_gates_open_is_silent(self, tmp_path: Path) -> None:
        assert _warning({}, tmp_path) is None

    def test_for_machine_reads_the_environment(self) -> None:
        with patch.dict(os.environ, {"DISABLE_GROWTHBOOK": "1"}):
            text = StartupWarning.for_machine(platform="win32").text()
        assert text is not None
        assert "DISABLE_GROWTHBOOK is set" in text

    def test_flag_and_policy_reasons_join(self, tmp_path: Path) -> None:
        _write(tmp_path, "managed-settings.json", {})
        text = _warning({"DO_NOT_TRACK": "1"}, tmp_path)
        assert text is not None
        assert "DO_NOT_TRACK is set" in text
        assert "channelsEnabled" in text


BASE: dict[str, object] = {
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": "Biff session starting.",
    }
}


class TestSessionStartOutput:
    """The hook tells the user (systemMessage) and the model (context)."""

    def test_closed_gate_reaches_user_and_model(self) -> None:
        warning = "biff: channels are not live in this session (x). " + POLL
        with patch.object(StartupWarning, "text", return_value=warning):
            out = StartupWarning.add_to(BASE)
        assert out["systemMessage"] == warning
        specific = cast("dict[str, str]", out["hookSpecificOutput"])
        assert specific["hookEventName"] == "SessionStart"
        assert specific["additionalContext"] == f"Biff session starting. {warning}"

    def test_open_gates_leave_the_output_unchanged(self) -> None:
        with patch.object(StartupWarning, "text", return_value=None):
            assert StartupWarning.add_to(BASE) == BASE

    def test_input_is_not_mutated(self) -> None:
        with patch.object(StartupWarning, "text", return_value="warn"):
            StartupWarning.add_to(BASE)
        assert "systemMessage" not in BASE


class TestHookEntryPoints:
    """Both SessionStart entry points carry the warning."""

    @pytest.mark.parametrize(
        "entry", [_cc_session_start, _cc_session_resume], ids=["start", "resume"]
    )
    def test_warning_is_emitted(
        self,
        entry: Callable[[], None],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with (
            patch("biff.plugins.claude_code.hook._read_hook_input", return_value={}),
            patch("biff.plugins.claude_code.hook._open_session"),
            patch.object(StartupWarning, "text", return_value="warn"),
        ):
            entry()
        out = json.loads(capsys.readouterr().out)
        assert out["systemMessage"] == "warn"
        assert out["hookSpecificOutput"]["additionalContext"].endswith(" warn")
