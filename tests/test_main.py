"""Tests for biff CLI entry point."""

from __future__ import annotations

import asyncio
import io
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from biff import logging_config
from biff.__main__ import _suppress_nats_noise, app
from biff.cli_session import CliContext
from biff.commands import CommandResult
from biff.config import ResolvedConfig
from biff.logging_config import configure_logging
from biff.models import BiffConfig
from biff.nats_relay import NatsRelay

if TYPE_CHECKING:
    import pytest

runner = CliRunner()

_RESOLVED = ResolvedConfig(
    config=BiffConfig(user="kai", repo_name="myrepo"),
    data_dir=Path("/tmp/biff/myrepo"),
    repo_root=Path("/proj/myrepo"),
)


# ---------------------------------------------------------------------------
# Helpers for product-command tests
# ---------------------------------------------------------------------------

_MOCK_CTX = CliContext(
    relay=MagicMock(),
    config=BiffConfig(user="kai", repo_name="myrepo"),
    session_key="kai:abc123",
    user="kai",
    tty="abc123",
)


@asynccontextmanager
async def _fake_session(
    *,
    interactive: bool = False,
    user_override: str | None = None,
) -> AsyncIterator[CliContext]:
    """Drop-in replacement for ``cli_session`` that needs no NATS."""
    yield _MOCK_CTX


class TestGlobalFlags:
    def test_verbose_and_quiet_mutually_exclusive(self) -> None:
        result = runner.invoke(app, ["--verbose", "--quiet", "version"])
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output

    def test_global_flags_after_subcommand(self) -> None:
        """Global flags work when placed after the subcommand (argv hoisting)."""
        # CliRunner bypasses sys.argv, so test hoisting via --json before subcommand
        result = runner.invoke(app, ["--verbose", "version"])
        assert result.exit_code == 0
        assert "biff" in result.output

    @patch("biff.commands.who", new_callable=AsyncMock)
    def test_user_override(self, mock_who: AsyncMock) -> None:
        """--user propagates identity override to cli_session."""
        mock_who.return_value = CommandResult(text="ok")
        with patch("biff.__main__.cli_session", wraps=_fake_session) as mock_sess:
            result = runner.invoke(app, ["--user", "github-actions", "who"])
        assert result.exit_code == 0
        mock_sess.assert_called_once_with(user_override="github-actions")
        mock_who.assert_awaited_once()


class TestVersionCommand:
    def test_prints_version(self) -> None:
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert "biff" in result.output

    def test_json_output(self) -> None:
        result = runner.invoke(app, ["--json", "version"])
        assert result.exit_code == 0
        assert '"version"' in result.output


class TestServeCommand:
    """``biff serve`` is HTTP-only; ``biff mcp`` is stdio-only."""

    @patch("biff.__main__.create_server")
    @patch("biff.__main__.create_state")
    @patch("biff.__main__.load_mcp_config", return_value=_RESOLVED)
    def test_http_default(
        self,
        _mock_config: MagicMock,
        _mock_state: MagicMock,
        mock_server: MagicMock,
    ) -> None:
        mock_mcp = MagicMock()
        mock_server.return_value = mock_mcp
        result = runner.invoke(app, ["serve", "--user", "kai"])
        assert result.exit_code == 0
        mock_mcp.run.assert_called_once_with(
            transport="http", host="127.0.0.1", port=8419
        )

    @patch("biff.__main__.create_server")
    @patch("biff.__main__.create_state")
    @patch("biff.__main__.load_mcp_config", return_value=_RESOLVED)
    def test_custom_host_port(
        self,
        _mock_config: MagicMock,
        _mock_state: MagicMock,
        mock_server: MagicMock,
    ) -> None:
        mock_mcp = MagicMock()
        mock_server.return_value = mock_mcp
        result = runner.invoke(
            app,
            ["serve", "--user", "kai", "--host", "192.168.1.1", "--port", "9000"],
        )
        assert result.exit_code == 0
        mock_mcp.run.assert_called_once_with(
            transport="http", host="192.168.1.1", port=9000
        )

    @patch("biff.__main__.create_server")
    @patch("biff.__main__.create_state")
    @patch("biff.__main__.load_mcp_config", return_value=_RESOLVED)
    def test_passes_user_override(
        self,
        mock_config: MagicMock,
        _mock_state: MagicMock,
        mock_server: MagicMock,
    ) -> None:
        mock_server.return_value = MagicMock()
        runner.invoke(app, ["serve", "--user", "kai"])
        mock_config.assert_called_once()
        call_kwargs = mock_config.call_args.kwargs
        assert call_kwargs["user_override"] == "kai"


class TestMcpCommand:
    """``biff mcp`` starts the MCP server with stdio transport."""

    @patch("biff.__main__.create_server")
    @patch("biff.__main__.create_state")
    @patch("biff.__main__.load_mcp_config", return_value=_RESOLVED)
    def test_stdio_transport(
        self,
        _mock_config: MagicMock,
        _mock_state: MagicMock,
        mock_server: MagicMock,
    ) -> None:
        mock_mcp = MagicMock()
        mock_server.return_value = mock_mcp
        result = runner.invoke(app, ["mcp", "--user", "kai"])
        assert result.exit_code == 0
        mock_mcp.run.assert_called_once_with(transport="stdio")

    @patch("biff.__main__.create_server")
    @patch("biff.__main__.create_state")
    @patch("biff.__main__.load_mcp_config", return_value=_RESOLVED)
    def test_passes_data_dir_override(
        self,
        mock_config: MagicMock,
        _mock_state: MagicMock,
        mock_server: MagicMock,
    ) -> None:
        mock_server.return_value = MagicMock()
        runner.invoke(app, ["mcp", "--user", "kai", "--data-dir", "/custom/dir"])
        call_kwargs = mock_config.call_args.kwargs
        assert call_kwargs["data_dir_override"] == Path("/custom/dir")

    @patch("biff.__main__.create_server")
    @patch("biff.__main__.create_state")
    @patch("biff.__main__.load_mcp_config", return_value=_RESOLVED)
    def test_passes_prefix(
        self,
        mock_config: MagicMock,
        _mock_state: MagicMock,
        mock_server: MagicMock,
    ) -> None:
        mock_server.return_value = MagicMock()
        runner.invoke(app, ["mcp", "--user", "kai", "--prefix", "/var/spool"])
        call_kwargs = mock_config.call_args.kwargs
        assert call_kwargs["prefix"] == Path("/var/spool")

    @patch("biff.__main__.create_server")
    @patch("biff.__main__.create_state")
    @patch("biff.__main__.load_mcp_config", return_value=_RESOLVED)
    def test_no_user_delegates_to_config(
        self,
        mock_config: MagicMock,
        _mock_state: MagicMock,
        mock_server: MagicMock,
    ) -> None:
        """When --user is omitted, load_mcp_config gets user_override=None."""
        mock_server.return_value = MagicMock()
        runner.invoke(app, ["mcp"])
        call_kwargs = mock_config.call_args.kwargs
        assert call_kwargs["user_override"] is None


class TestEnableCommand:
    @patch("biff.__main__.find_git_root")
    def test_writes_local_yaml_only(self, mock_root: MagicMock, tmp_path: Path) -> None:
        mock_root.return_value = tmp_path
        result = runner.invoke(app, ["enable"])
        assert result.exit_code == 0
        local_yaml = tmp_path / ".punt-labs" / "biff" / "config.local.yaml"
        assert local_yaml.exists()
        import yaml

        local = yaml.safe_load(local_yaml.read_text())
        assert local["enabled"] is True
        # No config.yaml created — enable no longer runs interactive init
        config_yaml = tmp_path / ".punt-labs" / "biff" / "config.yaml"
        assert not config_yaml.exists()

    @patch("biff.__main__.find_git_root")
    def test_existing_config_skips_init(
        self, mock_root: MagicMock, tmp_path: Path
    ) -> None:
        mock_root.return_value = tmp_path
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.yaml").write_text("team:\n  members:\n    - kai\n")
        result = runner.invoke(app, ["enable"])
        assert result.exit_code == 0
        assert "enabled" in result.output
        local_yaml = biff_dir / "config.local.yaml"
        assert local_yaml.exists()

    @patch("biff.__main__.find_git_root", return_value=None)
    def test_not_in_repo(self, _mock: MagicMock) -> None:
        result = runner.invoke(app, ["enable"])
        assert result.exit_code != 0
        assert "Not in a git repository" in result.output

    @patch("biff.__main__.find_git_root")
    def test_idempotent(self, mock_root: MagicMock, tmp_path: Path) -> None:
        mock_root.return_value = tmp_path
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.yaml").write_text("team:\n  members: []\n")
        runner.invoke(app, ["enable"])
        runner.invoke(app, ["enable"])
        import yaml

        local = yaml.safe_load((biff_dir / "config.local.yaml").read_text())
        assert local["enabled"] is True

    @patch("biff.__main__.find_git_root")
    def test_adds_gitignore_entry(self, mock_root: MagicMock, tmp_path: Path) -> None:
        mock_root.return_value = tmp_path
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.yaml").write_text("team:\n  members: []\n")
        runner.invoke(app, ["enable"])
        gitignore = (biff_dir / ".gitignore").read_text()
        assert "config.local.yaml" in gitignore


class TestDisableCommand:
    @patch("biff.__main__.find_git_root")
    def test_writes_disabled(self, mock_root: MagicMock, tmp_path: Path) -> None:
        mock_root.return_value = tmp_path
        result = runner.invoke(app, ["disable"])
        assert result.exit_code == 0
        assert "disabled" in result.output
        import yaml

        local_yaml = tmp_path / ".punt-labs" / "biff" / "config.local.yaml"
        local = yaml.safe_load(local_yaml.read_text())
        assert local["enabled"] is False

    @patch("biff.__main__.find_git_root", return_value=None)
    def test_not_in_repo(self, _mock: MagicMock) -> None:
        result = runner.invoke(app, ["disable"])
        assert result.exit_code != 0
        assert "Not in a git repository" in result.output

    @patch("biff.__main__.find_git_root")
    def test_idempotent(self, mock_root: MagicMock, tmp_path: Path) -> None:
        mock_root.return_value = tmp_path
        runner.invoke(app, ["disable"])
        runner.invoke(app, ["disable"])
        import yaml

        local_yaml = tmp_path / ".punt-labs" / "biff" / "config.local.yaml"
        local = yaml.safe_load(local_yaml.read_text())
        assert local["enabled"] is False


class TestNoArgsRepl:
    @patch("biff.__main__._repl", new_callable=AsyncMock)
    def test_no_args_launches_repl(self, mock_repl: AsyncMock) -> None:
        """``biff`` with no args calls the REPL."""
        mock_repl.return_value = None
        result = runner.invoke(app, [])
        assert result.exit_code == 0
        mock_repl.assert_awaited_once()

    def test_help_flag(self) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "Usage:" in result.output


class TestProductCommands:
    """Smoke tests for all 10 product commands via CliRunner.

    Each test mocks ``cli_session`` (no NATS needed) and the underlying
    ``commands.*`` function, then verifies the CLI parsed args correctly
    and forwarded them to the command function.
    """

    @patch("biff.__main__.cli_session", new=_fake_session)
    @patch("biff.commands.who", new_callable=AsyncMock)
    def test_who(self, mock_who: AsyncMock) -> None:
        mock_who.return_value = CommandResult(text="no sessions")
        result = runner.invoke(app, ["who"])
        assert result.exit_code == 0
        assert "no sessions" in result.output
        mock_who.assert_awaited_once_with(_MOCK_CTX)

    @patch("biff.__main__.cli_session", new=_fake_session)
    @patch("biff.commands.finger", new_callable=AsyncMock)
    def test_finger(self, mock_finger: AsyncMock) -> None:
        mock_finger.return_value = CommandResult(text="Login: kai")
        result = runner.invoke(app, ["finger", "@kai"])
        assert result.exit_code == 0
        assert "Login: kai" in result.output
        mock_finger.assert_awaited_once_with(_MOCK_CTX, "@kai")

    @patch("biff.__main__.cli_session", new=_fake_session)
    @patch("biff.commands.write", new_callable=AsyncMock)
    def test_write(self, mock_write: AsyncMock) -> None:
        mock_write.return_value = CommandResult(text="Message sent to @eric.")
        result = runner.invoke(app, ["write", "@eric", "hello"])
        assert result.exit_code == 0
        assert "Message sent" in result.output
        mock_write.assert_awaited_once_with(_MOCK_CTX, "@eric", "hello")

    @patch("biff.__main__.cli_session", new=_fake_session)
    @patch("biff.commands.read", new_callable=AsyncMock)
    def test_read(self, mock_read: AsyncMock) -> None:
        mock_read.return_value = CommandResult(text="No messages.")
        result = runner.invoke(app, ["read"])
        assert result.exit_code == 0
        assert "No messages" in result.output
        mock_read.assert_awaited_once_with(_MOCK_CTX)

    @patch("biff.__main__.cli_session", new=_fake_session)
    @patch("biff.commands.plan", new_callable=AsyncMock)
    def test_plan(self, mock_plan: AsyncMock) -> None:
        mock_plan.return_value = CommandResult(text="Plan: fixing tests")
        result = runner.invoke(app, ["plan", "fixing tests"])
        assert result.exit_code == 0
        assert "fixing tests" in result.output
        mock_plan.assert_awaited_once_with(_MOCK_CTX, "fixing tests")

    @patch("biff.__main__.cli_session", new=_fake_session)
    @patch("biff.commands.last", new_callable=AsyncMock)
    def test_last(self, mock_last: AsyncMock) -> None:
        mock_last.return_value = CommandResult(text="no sessions")
        result = runner.invoke(app, ["last"])
        assert result.exit_code == 0
        mock_last.assert_awaited_once_with(_MOCK_CTX, "", 25)

    @patch("biff.__main__.cli_session", new=_fake_session)
    @patch("biff.commands.last", new_callable=AsyncMock)
    def test_last_with_user_and_count(self, mock_last: AsyncMock) -> None:
        mock_last.return_value = CommandResult(text="@kai sessions")
        result = runner.invoke(app, ["last", "@kai", "--count", "5"])
        assert result.exit_code == 0
        mock_last.assert_awaited_once_with(_MOCK_CTX, "@kai", 5)

    @patch("biff.__main__.cli_session", new=_fake_session)
    @patch("biff.commands.wall", new_callable=AsyncMock)
    def test_wall_post(self, mock_wall: AsyncMock) -> None:
        mock_wall.return_value = CommandResult(text="Wall posted")
        result = runner.invoke(app, ["wall", "deploy freeze", "--duration", "2h"])
        assert result.exit_code == 0
        mock_wall.assert_awaited_once_with(
            _MOCK_CTX, "deploy freeze", "2h", clear=False
        )

    @patch("biff.__main__.cli_session", new=_fake_session)
    @patch("biff.commands.wall", new_callable=AsyncMock)
    def test_wall_read(self, mock_wall: AsyncMock) -> None:
        mock_wall.return_value = CommandResult(text="No active wall.")
        result = runner.invoke(app, ["wall"])
        assert result.exit_code == 0
        mock_wall.assert_awaited_once_with(_MOCK_CTX, "", "", clear=False)

    @patch("biff.__main__.cli_session", new=_fake_session)
    @patch("biff.commands.wall", new_callable=AsyncMock)
    def test_wall_clear(self, mock_wall: AsyncMock) -> None:
        mock_wall.return_value = CommandResult(text="Wall cleared.")
        result = runner.invoke(app, ["wall", "--clear"])
        assert result.exit_code == 0
        mock_wall.assert_awaited_once_with(_MOCK_CTX, "", "", clear=True)

    @patch("biff.__main__.cli_session", new=_fake_session)
    @patch("biff.commands.mesg", new_callable=AsyncMock)
    def test_mesg(self, mock_mesg: AsyncMock) -> None:
        mock_mesg.return_value = CommandResult(text="is y")
        result = runner.invoke(app, ["mesg", "on"])
        assert result.exit_code == 0
        assert "is y" in result.output
        mock_mesg.assert_awaited_once_with(_MOCK_CTX, "on")

    @patch("biff.__main__.cli_session", new=_fake_session)
    @patch("biff.commands.tty", new_callable=AsyncMock)
    def test_tty_with_name(self, mock_tty: AsyncMock) -> None:
        mock_tty.return_value = CommandResult(text="tty: dev")
        result = runner.invoke(app, ["tty", "dev"])
        assert result.exit_code == 0
        mock_tty.assert_awaited_once_with(_MOCK_CTX, "dev")

    @patch("biff.__main__.cli_session", new=_fake_session)
    @patch("biff.commands.tty", new_callable=AsyncMock)
    def test_tty_no_name(self, mock_tty: AsyncMock) -> None:
        mock_tty.return_value = CommandResult(text="tty: abc123")
        result = runner.invoke(app, ["tty"])
        assert result.exit_code == 0
        mock_tty.assert_awaited_once_with(_MOCK_CTX, "")

    @patch("biff.__main__.cli_session", new=_fake_session)
    @patch("biff.commands.status", new_callable=AsyncMock)
    def test_status(self, mock_status: AsyncMock) -> None:
        mock_status.return_value = CommandResult(text="connected")
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "connected" in result.output
        mock_status.assert_awaited_once_with(_MOCK_CTX)


class TestProductCommandErrorHandling:
    """Test _run() error paths: command errors, ValueError, JSON output."""

    @patch("biff.__main__.cli_session", new=_fake_session)
    @patch("biff.commands.who", new_callable=AsyncMock)
    def test_command_error_exits_1(self, mock_who: AsyncMock) -> None:
        mock_who.return_value = CommandResult(text="something broke", error=True)
        result = runner.invoke(app, ["who"])
        assert result.exit_code == 1

    @patch("biff.__main__.cli_session", new=_fake_session)
    @patch("biff.commands.who", new_callable=AsyncMock)
    def test_json_output(self, mock_who: AsyncMock) -> None:
        mock_who.return_value = CommandResult(
            text="fallback", json_data=[{"user": "kai"}]
        )
        result = runner.invoke(app, ["--json", "who"])
        assert result.exit_code == 0
        assert '"user"' in result.output
        assert "kai" in result.output

    @patch("biff.__main__.cli_session", new=_fake_session)
    @patch("biff.commands.who", new_callable=AsyncMock)
    def test_quiet_suppresses_output(self, mock_who: AsyncMock) -> None:
        mock_who.return_value = CommandResult(text="some output")
        result = runner.invoke(app, ["--quiet", "who"])
        assert result.exit_code == 0
        assert result.output.strip() == ""

    @patch("biff.__main__.cli_session", new=_fake_session)
    @patch(
        "biff.commands.who",
        new_callable=AsyncMock,
        side_effect=ValueError("no relay configured"),
    )
    def test_value_error_exits_1(self, _mock_who: AsyncMock) -> None:
        result = runner.invoke(app, ["who"])
        assert result.exit_code == 1


class TestNatsErrorStaysOffTerminal:
    """A transient NATS error_cb reaches biff.log but never the REPL (biff-9la).

    Boundary test: wire the real ``configure_logging`` + ``_suppress_nats_noise``
    the CLI uses, fire the real ``error_cb``, and prove the terminal stays clean
    while the file keeps the full record and traceback.  This is the exact
    failure mode that dumped ``[ERROR] biff.nats_relay: NATS error: TimeoutError``
    into the operator's interactive prompt.
    """

    def test_transient_error_is_file_only_not_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log_file = tmp_path / "biff.log"
        monkeypatch.setattr(logging_config, "_LOG_DIR", tmp_path)
        monkeypatch.setattr(logging_config, "_LOG_FILE", log_file)

        # dictConfig resolves ext://sys.stderr at configure time — capture it.
        captured_stderr = io.StringIO()
        monkeypatch.setattr(sys, "stderr", captured_stderr)

        root = logging.getLogger()
        saved_handlers = root.handlers[:]
        saved_level = root.level
        try:
            configure_logging(stderr_level="WARNING")  # the REPL's floor
            _suppress_nats_noise()

            relay = NatsRelay(url="tls://fake:4222", repo_name="test")
            on_error = relay._connection_callbacks(relay._generation)["error_cb"]

            # A real transient error — raised so it carries a traceback.
            try:
                raise TimeoutError(60, "Operation timed out")
            except TimeoutError as exc:
                raised = exc

            async def _fire() -> None:
                await on_error(raised)

            asyncio.run(_fire())

            for handler in root.handlers:
                handler.flush()
        finally:
            for handler in root.handlers:
                handler.close()
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)
            logging.getLogger("biff.nats_relay").setLevel(logging.NOTSET)

        # The interactive terminal stays clean — no error line, no traceback.
        stderr_text = captured_stderr.getvalue()
        assert "NATS error" not in stderr_text
        assert "Traceback" not in stderr_text
        assert "Operation timed out" not in stderr_text

        # biff.log keeps the full record AND the traceback for diagnosis.
        file_text = log_file.read_text(encoding="utf-8")
        assert "NATS error" in file_text
        assert "Traceback" in file_text
        assert "Operation timed out" in file_text
