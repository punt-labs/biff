"""Start-up warning that biff channels cannot be live (DES-087)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar, Protocol, cast
from unittest.mock import patch

import pytest

from biff._stdlib import find_git_root
from biff.plugins.claude_code import _hook_entry, hook
from biff.plugins.claude_code.channel_gates import ChannelGate, ClosedGate, EnvGate
from biff.plugins.claude_code.launch_flag import LaunchFlagGate
from biff.plugins.claude_code.managed_policy import ManagedPolicy
from biff.plugins.claude_code.managed_sources import ManagedSources
from biff.plugins.claude_code.poll_command import PollCommand
from biff.plugins.claude_code.session_output import SessionOutput
from biff.plugins.claude_code.startup_warning import StartupWarning
from biff.plugins.claude_code.warning_claim import WarningClaim

DEV = "--dangerously-load-development-channels"
BIFF = "plugin:biff@punt-labs"
POLL = "If you are not already polling, run /biff:read 2m."
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


class _UnknownGate:
    def closed(self) -> tuple[ClosedGate, ...]:
        return (
            ClosedGate(
                "launch flags could not be checked", "", notify_user=False, unknown=True
            ),
        )


class TestUncertainty:
    """The opener matches what is known."""

    def test_unknown_only_says_may_not_be_live(self) -> None:
        assert StartupWarning((_UnknownGate(),)).text() == (
            "biff: channels may not be live in this session "
            "(launch flags could not be checked). "
            "If new messages do not arrive on their own, run /biff:read 2m."
        )

    def test_a_known_closure_wins_over_an_unknown_one(self) -> None:
        gates: tuple[ChannelGate, ...] = (
            _UnknownGate(),
            *EnvGate.opt_outs({"DISABLE_TELEMETRY": "1"}),
        )
        text = StartupWarning(gates).text()
        assert text is not None
        assert text.startswith("biff: channels are not live in this session (")
        assert "could not be checked" not in text


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


class _CountingGate:
    def __init__(self) -> None:
        self.calls = 0

    def closed(self) -> tuple[ClosedGate, ...]:
        self.calls += 1
        return (ClosedGate("counted", "fix"),)


class TestGuardedAssembly:
    """One failing part silences only itself."""

    ENV: ClassVar[dict[str, str]] = {"DISABLE_TELEMETRY": "1"}

    def test_policy_failure_leaves_env_and_launch_gates_working(self) -> None:
        with patch.object(ManagedPolicy, "for_machine", side_effect=AttributeError):
            text = StartupWarning.for_machine(self.ENV, "linux").text()
        assert text is not None
        assert "DISABLE_TELEMETRY is set" in text

    def test_gates_are_evaluated_once_per_apply(self) -> None:
        gate = _CountingGate()
        StartupWarning((gate,)).apply(BASE, {"source": "startup"})
        assert gate.calls == 1


class TestPollCommand:
    """The poll command is the loaded plugin variant's."""

    @pytest.mark.parametrize(
        ("name", "command"),
        [
            (None, "/biff:read 2m"),
            ("", "/biff:read 2m"),
            ("biff", "/biff:read 2m"),
            ("biff-dev", "/biff-dev:read-dev 2m"),
        ],
    )
    def test_command(self, name: str | None, command: str) -> None:
        env = {} if name is None else {"BIFF_PLUGIN_NAME": name}
        assert PollCommand.for_env(env).text == command


class TestSessionOutputFailure:
    """A crash in the warning machinery still tells the model."""

    def test_the_model_is_told_the_check_failed(self) -> None:
        with patch.object(StartupWarning, "for_machine", side_effect=OSError("boom")):
            out = SessionOutput.warned(BASE, {"source": "startup"})
        assert "systemMessage" not in out
        assert _text(out) == (
            "Biff session starting. biff could not check whether channels are "
            "live; if messages do not arrive, run /biff:read 2m."
        )


class TestWarningClaim:
    """Two biff plugins in one session warn once."""

    def test_only_the_first_claim_wins(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PID": "4242"}
        data: dict[str, object] = {"session_id": "s", "source": "startup"}
        first = WarningClaim(tmp_path, env, data)
        second = WarningClaim(tmp_path, env, data)
        assert first.take() is True
        assert second.take() is False

    def test_other_sessions_and_sources_claim_separately(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PID": "4242"}
        base: dict[str, object] = {"session_id": "s", "source": "startup"}
        assert WarningClaim(tmp_path, env, base).take()
        assert WarningClaim(tmp_path, env, {**base, "session_id": "t"}).take()
        assert WarningClaim(tmp_path, env, {**base, "source": "compact"}).take()
        assert WarningClaim(tmp_path, {"CLAUDE_PID": "1"}, base).take()

    def test_two_compactions_minutes_apart_both_warn(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PID": "4242"}
        data: dict[str, object] = {"session_id": "s", "source": "compact"}
        first = WarningClaim(tmp_path, env, data, lambda: 1000.0)
        later = WarningClaim(tmp_path, env, data, lambda: 1000.0 + 180)
        assert first.take() is True
        assert later.take() is True

    def test_simultaneous_runs_warn_once(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PID": "4242"}
        data: dict[str, object] = {"session_id": "s", "source": "compact"}
        plugins = [WarningClaim(tmp_path, env, data, lambda: 1000.0) for _ in range(2)]
        assert [plugin.take() for plugin in plugins] == [True, False]

    def test_marker_is_private(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PID": "4242"}
        WarningClaim(tmp_path, env, {"session_id": "s", "source": "x"}).take()
        (marker,) = tmp_path.iterdir()
        assert marker.stat().st_mode & 0o777 == 0o600

    def test_no_session_id_cannot_dedupe(self, tmp_path: Path) -> None:
        claim = WarningClaim(tmp_path, {}, {})
        assert claim.take() and claim.take()

    def test_old_markers_are_pruned(self, tmp_path: Path) -> None:
        stale = tmp_path / "old"
        stale.write_text("")
        os.utime(stale, (0, 0))
        WarningClaim(tmp_path, {}, {"session_id": "s"}).take()
        assert not stale.exists()

    def test_apply_emits_once(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PID": "4242"}
        data: dict[str, object] = {"session_id": "s", "source": "startup"}
        warning = StartupWarning(EnvGate.opt_outs({"DISABLE_TELEMETRY": "1"}))
        first = warning.apply(BASE, data, WarningClaim(tmp_path, env, data).take)
        second = warning.apply(BASE, data, WarningClaim(tmp_path, env, data).take)
        assert "systemMessage" in first
        assert second == BASE


class TestApply:
    """User message on startup only; model context always."""

    def _apply(self, source: object) -> dict[str, object]:
        warning = StartupWarning(EnvGate.opt_outs({"DISABLE_TELEMETRY": "1"}))
        return warning.apply(BASE, {"source": source})

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
        assert StartupWarning(()).apply(BASE, {"source": "startup"}) == BASE

    def test_input_is_not_mutated(self) -> None:
        self._apply("startup")
        assert "systemMessage" not in BASE

    def test_model_only_gate_sends_no_system_message_on_startup(self) -> None:
        out = StartupWarning((_SilentGate(),)).apply(BASE, {"source": "startup"})
        assert "systemMessage" not in out
        assert "quiet reason" in json.dumps(out)


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
        text = _text(json.loads(capsys.readouterr().out))
        assert text.startswith("Biff session starting. biff could not check")


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
        same_session: bool = False,
    ) -> dict[str, object]: ...


def _text(out: dict[str, object]) -> str:
    return cast("dict[str, str]", out["hookSpecificOutput"])["additionalContext"]


def _ignore(_data: dict[str, object]) -> None:
    return None


@pytest.fixture
def run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> _Run:
    """Run the real entry point; only the process and file boundaries are faked."""
    runs = [0]
    for name in (*FLAGS, *PROVIDERS, "DO_NOT_TRACK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_PID", "4242")
    monkeypatch.setattr(hook, "_is_biff_enabled", lambda: True)
    monkeypatch.setattr(hook, "_open_session", _ignore)
    monkeypatch.setattr("biff.caller_stamp._live_ancestors", lambda: frozenset({4242}))

    def go(
        argv: list[str],
        *,
        managed: dict[str, object] | None = None,
        event: str = "session-start",
        source: str = "startup",
        same_session: bool = False,
    ) -> dict[str, object]:
        runs[0] += 0 if same_session else 1
        claims = tmp_path / f"claims{runs[0]}"

        def for_hook(
            _cls: type[WarningClaim], env: Mapping[str, str], data: dict[str, object]
        ) -> WarningClaim:
            return WarningClaim(claims, env, data)

        monkeypatch.setattr(WarningClaim, "for_hook", classmethod(for_hook))

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

    def test_channels_flag_with_an_allowlist_omitting_biff_is_model_only(
        self, run: _Run
    ) -> None:
        argv = ["claude", "--channels", BIFF]
        omits: dict[str, object] = {"allowedChannelPlugins": ["other@marketplace"]}
        listed: dict[str, object] = {"allowedChannelPlugins": ["biff@punt-labs"]}
        out = run(argv, managed=omits)
        assert "systemMessage" not in out
        assert "server-managed policy may still allow channels" in _text(out)
        assert "channels may not be live" in _text(out)
        quiet = run(argv, managed=listed)
        assert "systemMessage" not in quiet
        assert "may not be live" not in _text(quiet)

    def test_channels_flag_with_no_visible_allowlist_is_model_only(
        self, run: _Run
    ) -> None:
        enabled: dict[str, object] = {"channelsEnabled": True}
        for managed in (None, enabled):
            out = run(["claude", "--channels", BIFF], managed=managed)
            assert "systemMessage" not in out
            assert "server-managed policy may still allow channels" in _text(out)

    def test_local_channels_enabled_false_is_model_only(self, run: _Run) -> None:
        out = run(["claude", DEV, BIFF], managed={"channelsEnabled": False})
        assert "systemMessage" not in out
        assert "server-managed policy may still allow channels" in _text(out)

    def test_saved_poll_cadence_does_not_silence_the_warning(
        self, run: _Run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "repo"
        config = repo / ".punt-labs" / "biff"
        config.mkdir(parents=True)
        subprocess.run(  # noqa: S603
            ["git", "init", "--quiet", str(repo)],  # noqa: S607
            check=True,
        )
        (config / "config.local.yaml").write_text("poll_interval: 120\n")
        monkeypatch.chdir(repo)
        assert find_git_root() == repo.resolve()
        assert "systemMessage" in run(["claude"])

    def test_dev_plugin_gets_its_own_poll_command(
        self, run: _Run, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BIFF_PLUGIN_NAME", "biff-dev")
        out = run(["claude"])
        banner = cast("str", out["systemMessage"])
        assert "run /biff-dev:read-dev 2m" in banner
        assert "/biff:read" not in banner

    def test_prod_plugin_gets_the_prod_poll_command(
        self, run: _Run, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BIFF_PLUGIN_NAME", "biff")
        assert "run /biff:read 2m" in cast("str", run(["claude"])["systemMessage"])

    def test_two_plugins_warn_once(self, run: _Run) -> None:
        first = run(["claude"])
        second = run(["claude"], same_session=True)
        assert "systemMessage" in first
        assert "systemMessage" not in second
        assert "channels are not live" not in _text(second)

    def test_bedrock_env(self, run: _Run, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
        out = run(["claude", DEV, BIFF])
        banner = cast("str", out["systemMessage"])
        assert "channels need Anthropic authentication" in banner

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
