"""biff doctor names each environment variable that silently blocks channels."""

from __future__ import annotations

from pathlib import Path

import pytest

from biff.plugins.claude_code.channel_check_report import CheckTarget
from biff.plugins.claude_code.doctor_checks import DoctorChecks
from biff.plugins.claude_code.installer import ClaudeCodeInstaller

_NOWHERE = Path("/nonexistent/settings.json")
_OPT_OUTS = (
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "DISABLE_GROWTHBOOK",
    "DISABLE_TELEMETRY",
    "CLAUDE_CODE_DISABLE_FEATURE_FLAGS",
)


@pytest.mark.parametrize("name", _OPT_OUTS)
def test_each_opt_out_is_named_with_its_fix(name: str) -> None:
    (check,) = DoctorChecks(_NOWHERE, {name: "1"}).channels()
    assert not check.passed
    assert not check.required  # polling still delivers; doctor must not fail
    assert f"{name} is set" in check.message
    assert f"Unset {name} and restart Claude Code." in check.message


def test_a_cloud_provider_is_named() -> None:
    (check,) = DoctorChecks(_NOWHERE, {"CLAUDE_CODE_USE_BEDROCK": "1"}).channels()
    assert "channels need Anthropic authentication" in check.message
    assert "Polling" in check.message


def test_two_blockers_are_two_lines() -> None:
    env = {"DISABLE_TELEMETRY": "1", "CLAUDE_CODE_USE_VERTEX": "true"}
    assert len(DoctorChecks(_NOWHERE, env).channels()) == 2


@pytest.mark.parametrize("value", ["", "0", "false"])
def test_a_clean_environment_passes(value: str) -> None:
    (check,) = DoctorChecks(_NOWHERE, {"DISABLE_TELEMETRY": value}).channels()
    assert check.passed
    assert check.name == "Channels"
    # doctor sees only its own shell, not the one Claude Code was started from
    assert "this shell" in check.message
    assert "Claude Code's own environment" in check.message


def test_doctor_reports_the_shell_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISABLE_TELEMETRY", "1")
    messages = [c.message for c in ClaudeCodeInstaller().check()]
    assert any("DISABLE_TELEMETRY is set" in m for m in messages)


def test_all_ends_with_the_channel_check_and_it_is_informational() -> None:
    checks = DoctorChecks(_NOWHERE, {}, lambda: CheckTarget(None, "kai")).all()
    assert checks[-1].name == "Channel"
    assert "needs the NATS relay" in checks[-1].message
    assert not any(c.required for c in checks)
