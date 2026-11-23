"""Start-up warning that biff channels cannot be live (DES-087)."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Protocol, cast
from unittest.mock import patch

import pytest

from biff.plugins.claude_code import _hook_entry, hook
from biff.plugins.claude_code.channel_gates import ClosedGate, EnvGate
from biff.plugins.claude_code.launch_flag import LaunchFlagGate
from biff.plugins.claude_code.managed_sources import ManagedSources
from biff.plugins.claude_code.poll_setting import PollSetting
from biff.plugins.claude_code.startup_warning import StartupWarning

DEV = "--dangerously-load-development-channels"
BIFF = "plugin:biff@punt-labs"
POLL = "Run /biff:read 2m to poll."
FLAGS = (
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "DISABLE_GROWTHBOOK",
    "DISABLE_TELEMETRY",
    "CLAUDE_CODE_DISABLE_FEATURE_FLAGS",
)
PROVIDERS = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)
BASE: dict[str, object] = {
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": "Biff session starting.",
    }
}


def _env_warning(env: dict[str, str]) -> str | None:
    return StartupWarning((*EnvGate.opt_outs(env), EnvGate.provider(env))).text()


class TestEnvGates:
    """Feature-flag opt-outs and non-Anthropic providers close channels."""

    @pytest.mark.parametrize("flag", FLAGS)
    def test_each_flag_warns_by_name(self, flag: str) -> None:
        text = _env_warning({flag: "1"})
        assert text is not None
        assert text.startswith("biff: channels are not live in this session (")
        assert f"{flag} is set" in text
        assert "New messages will not arrive on their own." in text
        assert POLL in text
        assert f"Unset {flag} and restart Claude Code." in text

    def test_do_not_track_is_not_a_gate(self) -> None:
        assert _env_warning({"DO_NOT_TRACK": "1"}) is None

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_values_close_the_gate(self, value: str) -> None:
        assert _env_warning({"DISABLE_TELEMETRY": value}) is not None

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
    def test_falsy_values_leave_the_gate_open(self, value: str) -> None:
        assert _env_warning({"DISABLE_TELEMETRY": value}) is None

    @pytest.mark.parametrize("name", PROVIDERS)
    def test_provider_closes_channels(self, name: str) -> None:
        text = _env_warning({name: "1"})
        assert text is not None
        assert "channels need Anthropic authentication" in text
        assert name in text
        assert POLL in text

    def test_two_flags_name_both(self) -> None:
        text = _env_warning({"DISABLE_TELEMETRY": "1", "DISABLE_GROWTHBOOK": "1"})
        assert text is not None
        assert "DISABLE_TELEMETRY is set" in text
        assert "DISABLE_GROWTHBOOK is set" in text
        assert text.count(POLL) == 1


class _BrokenGate:
    def closed(self) -> tuple[ClosedGate, ...]:
        msg = "boom"
        raise RuntimeError(msg)


class _SilentGate:
    def closed(self) -> tuple[ClosedGate, ...]:
        return (ClosedGate("quiet reason", "quiet fix", notify_user=False),)


class TestStartupWarning:
    """The assembled warning."""

    def test_all_open_is_silent(self) -> None:
        assert _env_warning({}) is None

    def test_a_failing_gate_is_logged_and_skipped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        gates = (
            _BrokenGate(),
            EnvGate(("DISABLE_TELEMETRY",), {"DISABLE_TELEMETRY": "1"}),
        )
        text = StartupWarning(gates).text()
        assert text is not None
        assert "DISABLE_TELEMETRY is set" in text
        assert "_BrokenGate" in caplog.text

    def test_user_text_leaves_out_model_only_gates(self) -> None:
        warning = StartupWarning((_SilentGate(),))
        assert warning.text() is not None
        assert warning.text(user=True) is None

    def test_polling_on_silences_everything(self) -> None:
        env = {"DISABLE_TELEMETRY": "1"}
        gates = EnvGate.opt_outs(env)
        assert StartupWarning(gates, lambda: True).text() is None
        assert StartupWarning(gates, lambda: False).text() is not None


class TestApply:
    """User message on startup only; model context always."""

    def _apply(self, source: object) -> dict[str, object]:
        warning = StartupWarning(EnvGate.opt_outs({"DISABLE_TELEMETRY": "1"}))
        return warning.apply(BASE, source=source)

    def test_startup_reaches_user_and_model(self) -> None:
        out = self._apply("startup")
        text = cast("str", out["systemMessage"])
        specific = cast("dict[str, str]", out["hookSpecificOutput"])
        assert specific["additionalContext"] == f"Biff session starting. {text}"
        assert specific["hookEventName"] == "SessionStart"

    @pytest.mark.parametrize("source", ["resume", "compact", "clear", None])
    def test_other_sources_reach_only_the_model(self, source: object) -> None:
        out = self._apply(source)
        assert "systemMessage" not in out
        specific = cast("dict[str, str]", out["hookSpecificOutput"])
        assert "channels are not live" in specific["additionalContext"]

    def test_open_gates_leave_the_output_unchanged(self) -> None:
        assert StartupWarning(()).apply(BASE, source="startup") == BASE

    def test_input_is_not_mutated(self) -> None:
        self._apply("startup")
        assert "systemMessage" not in BASE

    def test_model_only_gate_sends_no_system_message_on_startup(self) -> None:
        out = StartupWarning((_SilentGate(),)).apply(BASE, source="startup")
        assert "systemMessage" not in out
        assert "quiet reason" in json.dumps(out)


class TestPollSetting:
    """Polling already started means the user needs no warning."""

    def _root(self, tmp_path: Path, body: str | None) -> Path:
        config = tmp_path / ".punt-labs" / "biff"
        config.mkdir(parents=True)
        if body is not None:
            (config / "config.local.yaml").write_text(body)
        return tmp_path

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            (None, False),
            ("other: 1\n", False),
            ("poll_interval: 0\n", False),
            ("poll_interval: 120\n", True),
            ("poll_interval: 2.5\n", True),
            ("poll_interval: nope\n", False),
        ],
    )
    def test_is_on(self, tmp_path: Path, body: str | None, expected: bool) -> None:
        assert PollSetting(self._root(tmp_path, body)).is_on() is expected

    def test_no_repository_is_not_polling(self) -> None:
        assert PollSetting(None).is_on() is False


class TestWarningFailure:
    """Biff's own context survives every failure of the warning path."""

    @pytest.fixture(autouse=True)
    def _session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hook, "_open_session", _ignore)
        monkeypatch.setattr(hook, "_read_hook_input", _startup_input)
        monkeypatch.setattr(hook, "handle_session_start", _start_context)

    @pytest.mark.parametrize("resume", [False, True])
    def test_failure_is_logged_and_base_context_emitted(
        self,
        resume: bool,
        capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with (
            caplog.at_level(logging.WARNING),
            patch.object(StartupWarning, "for_machine", side_effect=OSError("boom")),
        ):
            hook._run_session_start(resume=resume)
        out = json.loads(capsys.readouterr().out)
        assert "systemMessage" not in out
        assert _text(out).startswith(
            "Biff session " + ("resumed" if resume else "starting")
        )
        assert [r.levelname for r in caplog.records] == ["WARNING"]

    def test_import_failure_is_survived(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch.dict(
            sys.modules, {"biff.plugins.claude_code.startup_warning": None}
        ):
            hook._run_session_start(resume=False)
        assert _text(json.loads(capsys.readouterr().out)) == "Biff session starting."


def _startup_input() -> dict[str, object]:
    return {"source": "startup"}


def _start_context(_data: dict[str, object] | None = None) -> str:
    return "Biff session starting."


class _Run(Protocol):
    """Drive ``biff-hook claude-code <event>`` and return the hook's JSON."""

    def __call__(
        self,
        argv: list[str],
        *,
        managed: dict[str, object] | None = None,
        event: str = "session-start",
        source: str = "startup",
    ) -> dict[str, object]: ...


def _text(out: dict[str, object]) -> str:
    return cast("dict[str, str]", out["hookSpecificOutput"])["additionalContext"]


def _ignore(_data: dict[str, object]) -> None:
    return None


def _polling(_self: PollSetting) -> bool:
    return True


def _no_poll(_cls: type[PollSetting]) -> PollSetting:
    return PollSetting(None)


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> _Run:
    """Run the real entry point; only the process and file boundaries are faked."""
    for name in (*FLAGS, *PROVIDERS, "DO_NOT_TRACK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_PID", "4242")
    monkeypatch.setattr(hook, "_is_biff_enabled", lambda: True)
    monkeypatch.setattr(hook, "_open_session", _ignore)
    monkeypatch.setattr(PollSetting, "here", classmethod(_no_poll))
    monkeypatch.setattr("biff.caller_stamp._live_ancestors", lambda: frozenset({4242}))

    def go(
        argv: list[str],
        *,
        managed: dict[str, object] | None = None,
        event: str = "session-start",
        source: str = "startup",
    ) -> dict[str, object]:
        def read_argv(_pid: int) -> list[str]:
            return argv

        def sources(_platform: str) -> tuple[dict[str, object], ...]:
            return () if managed is None else (managed,)

        def read_input() -> dict[str, object]:
            return {"session_id": "s", "source": source}

        monkeypatch.setattr(LaunchFlagGate, "_process_argv", staticmethod(read_argv))
        monkeypatch.setattr(ManagedSources, "for_platform", staticmethod(sources))
        monkeypatch.setattr(hook, "_read_hook_input", read_input)
        monkeypatch.setattr(sys, "argv", ["biff-hook", "claude-code", event])
        _hook_entry.main()
        return cast("dict[str, object]", json.loads(capsys.readouterr().out))

    return go


class TestEntryPoint:
    """The real biff-hook entry point, with real gates."""

    def test_dev_flag_all_open_adds_nothing(self, run: _Run) -> None:
        out = run(["claude", DEV, BIFF])
        assert "systemMessage" not in out
        assert "channels are not live" not in _text(out)
        assert "Biff session starting." in _text(out)

    def test_no_flag_warns_user_and_model(self, run: _Run) -> None:
        out = run(["claude"])
        banner = cast("str", out["systemMessage"])
        assert "channels are not live" in banner
        assert banner in _text(out)

    def test_comma_form(self, run: _Run) -> None:
        assert "systemMessage" not in run(["claude", DEV, f"plugin:x@y,{BIFF}"])

    def test_dev_flag_naming_another_entry(self, run: _Run) -> None:
        out = run(["claude", DEV, "server:fakechat"])
        assert "channel flag for biff" in cast("str", out["systemMessage"])

    def test_print_mode_reaches_only_the_model(self, run: _Run) -> None:
        out = run(["claude", "-p", DEV, BIFF])
        assert "systemMessage" not in out
        assert "channels are not live" in _text(out)

    def test_unrelated_managed_file_is_no_finding(self, run: _Run) -> None:
        out = run(["claude", DEV, BIFF], managed={"permissions": {"allow": []}})
        assert "systemMessage" not in out

    def test_dev_flag_with_unlisted_policy_is_live(self, run: _Run) -> None:
        out = run(["claude", DEV, BIFF], managed={"channelsEnabled": True})
        assert "systemMessage" not in out

    def test_channels_flag_needs_the_allowlist(self, run: _Run) -> None:
        argv = ["claude", "--channels", BIFF]
        assert "systemMessage" in run(argv, managed={"channelsEnabled": True})
        listed: dict[str, object] = {
            "channelsEnabled": True,
            "allowedChannelPlugins": ["biff@punt-labs"],
        }
        assert "systemMessage" not in run(argv, managed=listed)

    def test_bedrock_env(self, run: _Run, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
        out = run(["claude", DEV, BIFF])
        banner = cast("str", out["systemMessage"])
        assert "channels need Anthropic authentication" in banner

    def test_polling_on_sends_nothing(
        self, run: _Run, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(PollSetting, "is_on", _polling)
        out = run(["claude"])
        assert "systemMessage" not in out
        assert "channels are not live" not in _text(out)

    def test_resume_sends_context_but_no_banner(self, run: _Run) -> None:
        out = run(["claude"], event="session-resume", source="resume")
        assert "systemMessage" not in out
        assert "channels are not live" in _text(out)

    def test_start_passes_the_hook_input_to_the_handler(self, run: _Run) -> None:
        seen: list[object] = []
        real = hook.handle_session_start

        def spy(data: dict[str, object] | None = None) -> str:
            seen.append(data)
            return real(data)

        with patch.object(hook, "handle_session_start", spy):
            run(["claude", DEV, BIFF])
        assert seen == [{"session_id": "s", "source": "startup"}]
