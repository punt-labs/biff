"""CI notifications must not report success having delivered nothing."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from biff.cli_context import CliContext
from biff.commands.wall import wall
from biff.doctor import _check_ci_workflow  # pyright: ignore[reportPrivateUsage]
from biff.models import BiffConfig
from biff.relay import LocalRelay
from biff.relay_check import LocalRelayCheck

_WORKFLOW = Path(".github") / "workflows" / "biff-notify.yml"


@pytest.fixture(autouse=True)
def _clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("BIFF_RELAY_URL", "BIFF_RELAY_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def _repo(tmp_path: Path, config: str | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)  # noqa: S603, S607
    if config is not None:
        (repo / ".punt-labs" / "biff").mkdir(parents=True)
        (repo / ".punt-labs" / "biff" / "config.yaml").write_text(config)
    return repo


def _install_workflow(repo: Path) -> None:
    (repo / _WORKFLOW).parent.mkdir(parents=True)
    (repo / _WORKFLOW).write_text("name: Biff CI Notifications\n")


class TestNotifyWarning:
    def test_installed_workflow_on_the_local_relay_warns(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        _install_workflow(repo)
        warning = LocalRelayCheck.notify_warning(repo)
        assert warning is not None
        assert "BIFF_RELAY_URL" in warning
        assert "local relay" in warning

    def test_no_workflow_no_warning(self, tmp_path: Path) -> None:
        assert LocalRelayCheck.notify_warning(_repo(tmp_path)) is None

    def test_a_committed_relay_means_no_warning(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, "relay:\n  type: nats\n  url: nats://relay:4222\n")
        _install_workflow(repo)
        assert LocalRelayCheck.notify_warning(repo) is None

    def test_doctor_flags_the_deployed_workflow(self, tmp_path: Path) -> None:
        from biff._cli_runtime import CliInvocation
        from biff.enablement import RepoEnablement

        repo = _repo(tmp_path)
        RepoEnablement(repo, CliInvocation.plugin_registry()).enable()
        with patch("biff.doctor.find_git_root", return_value=repo):
            result = _check_ci_workflow()
        assert not result.passed
        assert "BIFF_RELAY_URL" in result.message


class TestEnableWarns:
    def test_enable_prints_the_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from biff.__main__ import enable

        repo = _repo(tmp_path)
        enable(start=repo)
        assert "BIFF_RELAY_URL" in capsys.readouterr().out


class TestWallInCi:
    @pytest.fixture
    def ctx(self, tmp_path: Path) -> CliContext:
        return CliContext(
            relay=LocalRelay(tmp_path),
            config=BiffConfig(user="github-actions", repo_name="r"),
            session_key="github-actions:abc12345",
            user="github-actions",
            tty="abc12345",
        )

    async def test_wall_on_the_local_relay_in_ci_fails(
        self, ctx: CliContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        result = await wall(ctx, "CI failed", "2h", clear=False)
        assert result.error
        assert "vars.BIFF_RELAY_URL" in result.text
        assert await ctx.relay.get_wall() is None

    async def test_wall_outside_ci_is_unchanged(self, ctx: CliContext) -> None:
        result = await wall(ctx, "hello", "1h", clear=False)
        assert not result.error

    async def test_reading_the_wall_in_ci_is_not_blocked(
        self, ctx: CliContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        result = await wall(ctx, "", "", clear=False)
        assert not result.error


class TestNotifyWarningReadsWhatCiReads:
    """CI sees only the committed config.yaml, never your personal file or shell."""

    def test_a_personal_relay_does_not_silence_the_warning(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(tmp_path)
        _install_workflow(repo)
        (repo / ".punt-labs" / "biff").mkdir(parents=True)
        (repo / ".punt-labs" / "biff" / "config.local.yaml").write_text(
            "relay:\n  type: nats\n  url: nats://mine:4222\n"
        )
        assert LocalRelayCheck.notify_warning(repo) is not None

    def test_a_shell_override_does_not_silence_the_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(tmp_path)
        _install_workflow(repo)
        monkeypatch.setenv("BIFF_RELAY_URL", "nats://mine:4222")
        assert LocalRelayCheck.notify_warning(repo) is not None

    def test_a_committed_local_relay_also_needs_allow_env_override(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(tmp_path, "relay:\n  type: local\n")
        _install_workflow(repo)
        warning = LocalRelayCheck.notify_warning(repo)
        assert warning is not None
        assert "allow_env_override" in warning

    def test_a_committed_demo_relay_is_deliverable(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, "relay:\n  type: demo\n")
        _install_workflow(repo)
        assert LocalRelayCheck.notify_warning(repo) is None
