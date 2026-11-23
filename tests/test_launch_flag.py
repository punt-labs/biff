"""The joint launch-flag and policy gate: when can biff be a live channel."""

from __future__ import annotations

import os

import psutil
import pytest

from biff.plugins.claude_code.channel_gates import ClosedGate
from biff.plugins.claude_code.claude_argv import ClaudeArgv
from biff.plugins.claude_code.launch_flag import LaunchFlagGate
from biff.plugins.claude_code.managed_policy import ManagedPolicy

DEV = "--dangerously-load-development-channels"
BIFF = "plugin:biff@punt-labs"
ENV = {"CLAUDE_PID": "4242"}
LISTED = ManagedPolicy(({"allowedChannelPlugins": ["biff@punt-labs"]},))
UNLISTED = ManagedPolicy(())


def _closed(
    argv: list[str] | Exception,
    policy: ManagedPolicy = UNLISTED,
    *,
    env: dict[str, str] | None = None,
    ancestors: frozenset[int] = frozenset({4242}),
) -> tuple[ClosedGate, ...]:
    def read(_pid: int) -> list[str]:
        if isinstance(argv, Exception):
            raise argv
        return argv

    gate = LaunchFlagGate(ENV if env is None else env, policy, read, lambda: ancestors)
    return gate.closed()


class TestArgv:
    """Variadic flag values: spaced, comma-joined and equals-joined."""

    @pytest.mark.parametrize(
        "argv",
        [
            ["claude", DEV, BIFF],
            ["claude", f"{DEV}={BIFF}"],
            ["claude", DEV, f"plugin:x@y,{BIFF}"],
            ["claude", DEV, "plugin:x@y", BIFF, "--resume"],
            ["claude", f"{DEV}=plugin:x@y", BIFF],
            ["claude", DEV, "plugin:biff-dev@local"],
        ],
    )
    def test_dev_flag_naming_biff(self, argv: list[str]) -> None:
        assert ClaudeArgv(argv).dev_names_biff

    @pytest.mark.parametrize(
        "argv",
        [
            ["claude"],
            ["claude", DEV, "server:fakechat"],
            ["claude", DEV, "--resume", BIFF],
            ["claude", "--channels", BIFF],
            ["claude", BIFF],
        ],
    )
    def test_dev_flag_not_naming_biff(self, argv: list[str]) -> None:
        assert not ClaudeArgv(argv).dev_names_biff

    @pytest.mark.parametrize(
        "argv",
        [
            ["claude", "--channels", BIFF],
            ["claude", f"--channels={BIFF}"],
            ["claude", "--channels", f"plugin:x@y,{BIFF}"],
            ["claude", "--channels=plugin:x@y", BIFF],
        ],
    )
    def test_channels_flag_naming_biff(self, argv: list[str]) -> None:
        assert ClaudeArgv(argv).channels_name_biff

    def test_channels_flag_does_not_accept_biff_dev(self) -> None:
        assert not ClaudeArgv(
            ["claude", "--channels", "plugin:biff-dev@l"]
        ).channels_name_biff

    @pytest.mark.parametrize(("flag", "expected"), [("-p", True), ("--print", True)])
    def test_print_mode(self, flag: str, expected: bool) -> None:
        assert ClaudeArgv(["claude", flag, "hi"]).is_print is expected

    def test_not_print_mode(self) -> None:
        assert not ClaudeArgv(["claude", DEV, BIFF]).is_print


class TestJointGate:
    """Live only with the dev flag naming biff, or --channels plus the allowlist."""

    @pytest.mark.parametrize(
        "policy", [UNLISTED, LISTED, ManagedPolicy(({"allowedChannelPlugins": []},))]
    )
    def test_dev_flag_bypasses_the_allowlist(self, policy: ManagedPolicy) -> None:
        assert _closed(["claude", DEV, BIFF], policy) == ()

    def test_channels_with_the_allowlist_is_open(self) -> None:
        assert _closed(["claude", "--channels", BIFF], LISTED) == ()

    def test_channels_without_the_allowlist_is_closed(self) -> None:
        (gate,) = _closed(["claude", "--channels", BIFF], UNLISTED)
        assert "Anthropic's own" in gate.reason
        assert "allowedChannelPlugins" in gate.reason
        assert f"{DEV} {BIFF}" in gate.fix

    def test_dev_flag_naming_another_entry_is_closed(self) -> None:
        (gate,) = _closed(["claude", DEV, "server:fakechat"])
        assert "not started with a channel flag for biff" in gate.reason

    def test_no_flag_recommends_the_dev_flag(self) -> None:
        (gate,) = _closed(["claude"])
        assert gate.notify_user
        assert gate.fix.startswith(f"Restart Claude Code with {DEV} {BIFF}.")
        assert "--channels" in gate.fix
        assert "allowedChannelPlugins" in gate.fix

    def test_print_mode_is_closed_for_the_model_only(self) -> None:
        (gate,) = _closed(["claude", "-p", DEV, BIFF])
        assert not gate.notify_user
        assert "-p" in gate.reason


class TestUnknown:
    """An unreadable or untrusted argv is unknown: silent and logged."""

    @pytest.mark.parametrize("env", [{}, {"CLAUDE_PID": ""}, {"CLAUDE_PID": "abc"}])
    def test_no_usable_pid(
        self, env: dict[str, str], caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("INFO"):
            assert _closed(["claude"], env=env) == ()
        assert "launch_flag_unknown" in caplog.text

    def test_pid_that_is_not_an_ancestor(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("INFO"):
            assert _closed(["claude"], ancestors=frozenset({1})) == ()
        assert "launch_flag_unknown" in caplog.text

    @pytest.mark.parametrize(
        "error",
        [psutil.NoSuchProcess(4242), psutil.AccessDenied(4242), OSError(22, "x")],
    )
    def test_unreadable_process(
        self, error: Exception, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("INFO"):
            assert _closed(error) == ()
        assert "launch_flag_unknown" in caplog.text

    def test_default_readers_use_a_real_ancestor(self) -> None:
        env = {"CLAUDE_PID": str(os.getppid())}
        gate = LaunchFlagGate(env, UNLISTED)
        assert len(gate.closed()) == 1
