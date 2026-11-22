"""Private durable outcomes for checkout changes, HTTP shutdown, and outages."""

from __future__ import annotations

import logging
import signal
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import uvicorn
from fastmcp import FastMCP
from nats.js.errors import KeyNotFoundError
from punt_observe.logging import Role
from typer.testing import CliRunner

from biff.__main__ import app
from biff.enablement import RepoEnablement
from biff.logging_config import configure_logging
from biff.nats_relay import NatsRelay
from biff.server.entrypoints import McpServerOptions
from biff.server.transport_runtime import McpLifecycleHandler


@pytest.mark.parametrize(
    ("event", "handler", "args"),
    [
        ("post-commit", "handle_post_commit", []),
        ("post-checkout", "handle_post_checkout", ["old", "new", "1"]),
        ("pre-push", "handle_pre_push", ["origin"]),
    ],
)
def test_git_hook_fault_is_durable(
    event: str,
    handler: str,
    args: list[str],
    private_repo: Path,
    durable_log: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full CLI logs one private ERROR for each deployed Git hook crash."""
    marker = private_repo / ".punt-labs/biff/enabled"
    marker.parent.mkdir(parents=True)
    marker.touch()
    monkeypatch.chdir(private_repo)
    error = RuntimeError("secret-payload")
    with patch(f"biff.plugins.claude_code.hook.{handler}", side_effect=error):
        result = CliRunner().invoke(app, ["hook", "git", event, *args], input="")
    assert result.exit_code != 0
    assert result.exception is error
    assert result.stdout == ""
    records = durable_log.read_text().splitlines()
    assert len(records) == 1
    assert (
        "[ERROR] biff.plugins.claude_code._hook_cli: "
        "hook_cli_failed error=RuntimeError at " in records[0]
    )
    assert f"biff.plugins.claude_code.hook.git_{event.replace('-', '_')}:" in records[0]
    assert "secret-payload" not in records[0]
    assert durable_log.stat().st_mode & 0o777 == 0o600


def test_git_hook_disabled_is_silent(
    private_repo: Path, durable_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disabled repository remains a successful no-op with no log record."""
    monkeypatch.chdir(private_repo)
    result = CliRunner().invoke(app, ["hook", "git", "post-commit"])
    assert result.exit_code == 0
    assert durable_log.read_text() == ""


@pytest.fixture
def private_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "private-repository"
    repo.mkdir()
    subprocess.run(  # noqa: S603
        ["git", "init", "-q", str(repo)],  # noqa: S607
        check=True,
        capture_output=True,
    )
    return repo


@pytest.fixture
def durable_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("HOME", str(tmp_path))
    loggers = [
        logging.getLogger(),
        *(
            logger
            for logger in logging.root.manager.loggerDict.values()
            if isinstance(logger, logging.Logger)
        ),
    ]
    saved = [
        (logger, logger.level, logger.handlers[:], logger.propagate, logger.disabled)
        for logger in loggers
    ]
    original_handlers = {
        handler for _, _, handlers, _, _ in saved for handler in handlers
    }
    try:
        configure_logging(Role.MCP, stderr_level="WARNING")
        yield tmp_path / ".punt-labs/biff/logs/biff.log"
    finally:
        for logger in loggers:
            for handler in logger.handlers:
                if handler not in original_handlers:
                    handler.close()
        for logger, level, handlers, propagate, disabled in saved:
            logger.handlers[:] = handlers
            logger.setLevel(level)
            logger.propagate = propagate
            logger.disabled = disabled


@pytest.mark.parametrize("operation", ["enable", "disable"])
def test_checkout_outcome_changed_then_unchanged(
    operation: str, private_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manager = RepoEnablement(private_repo)
    if operation == "disable":
        manager.enable()
    caplog.set_level(logging.INFO, logger="biff.enablement")
    caplog.clear()
    action = manager.enable if operation == "enable" else manager.disable
    assert not action().failed
    assert not action().failed
    records = [r for r in caplog.records if r.name == "biff.enablement"]
    assert len(records) == 2
    assert f"operation={operation} outcome=changed " in records[0].message
    labels = records[0].message.split("changes=")[1].split(",")
    assert {"marker", "workflow", "hooks", "permissions"}.issubset(labels)
    assert (
        records[1].message
        == f"Enablement operation={operation} outcome=unchanged changes=none"
    )
    assert all(r.levelno == logging.INFO and r.exc_info is None for r in records)
    assert str(private_repo) not in caplog.text
    assert "private" not in caplog.text


@pytest.mark.parametrize("artifact", ["enabled", "CLAUDE.md"])
def test_enable_reports_marker_or_guide_only_changes(
    artifact: str, private_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manager = RepoEnablement(private_repo)
    manager.enable()
    (private_repo / ".punt-labs/biff" / artifact).unlink()
    caplog.clear()
    caplog.set_level(logging.INFO, logger="biff.enablement")
    manager.enable()
    label = "marker" if artifact == "enabled" else "guide"
    assert caplog.messages == [
        f"Enablement operation=enable outcome=changed changes={label}"
    ]


@pytest.mark.parametrize("operation", ["enable", "disable"])
@pytest.mark.parametrize("target_exists", [False, True])
def test_marker_symlinks_report_changed_without_touching_targets(
    operation: str,
    target_exists: bool,
    private_repo: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = RepoEnablement(private_repo)
    manager.enable()
    marker = private_repo / ".punt-labs/biff/enabled"
    marker.unlink()
    target = private_repo.parent / "private-target"
    if target_exists:
        target.write_text("private-content")
    marker.symlink_to(target)
    caplog.clear()
    caplog.set_level(logging.INFO, logger="biff.enablement")
    action = manager.enable if operation == "enable" else manager.disable
    assert not action().failed
    assert not marker.is_symlink()
    assert marker.is_file() is (operation == "enable")
    assert not action().failed
    assert target.exists() is target_exists
    if target_exists:
        assert target.read_text() == "private-content"
    assert len(caplog.records) == 2
    assert f"operation={operation} outcome=changed " in caplog.messages[0]
    assert "marker" in caplog.messages[0].split("changes=")[1].split(",")
    assert caplog.messages[1] == (
        f"Enablement operation={operation} outcome=unchanged changes=none"
    )
    assert "private" not in caplog.text


def test_enable_error_does_not_log_success_or_exception_content(
    private_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="biff.enablement")
    with (
        patch(
            "biff.enablement.deploy_ci_workflow", side_effect=OSError("private-error")
        ),
        pytest.raises(OSError),
    ):
        RepoEnablement(private_repo).enable()
    assert not [r for r in caplog.records if r.name == "biff.enablement"]
    assert "private" not in caplog.text


def test_cli_outcomes_reach_durable_file(
    private_repo: Path,
    durable_log: Path,
) -> None:
    with patch("biff.__main__.find_git_root", return_value=private_repo):
        for operation in ("enable", "enable", "disable", "disable"):
            result = CliRunner().invoke(app, [operation])
            assert result.exit_code == 0, result.output
    text = durable_log.read_text()
    records = [line for line in text.splitlines() if "Enablement operation=" in line]
    assert len(records) == 4
    for line, operation, outcome in zip(
        records,
        ("enable", "enable", "disable", "disable"),
        ("changed", "unchanged", "changed", "unchanged"),
        strict=True,
    ):
        assert f"operation={operation} outcome={outcome}" in line
        assert "[INFO]" in line
    assert str(private_repo) not in text
    assert "private" not in text


def test_stdio_shutdown_is_durable(durable_log: Path) -> None:
    """A normal return records one fixed stop outcome, even without tool calls."""
    with patch.object(McpServerOptions, "_observed_server") as observed:
        result = CliRunner().invoke(app, ["mcp", "--user", "private-user"])
    assert result.exit_code == 0
    observed.return_value.run.assert_called_once_with(transport="stdio")
    lines = durable_log.read_text().splitlines()
    stop = [line for line in lines if "shutdown outcome=" in line]
    assert len(stop) == 1
    assert stop[0].endswith(
        "[INFO] biff.server.transport_runtime: stdio MCP shutdown outcome=completed"
    )
    assert "private" not in durable_log.read_text()
    assert durable_log.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("error", [RuntimeError("private-payload"), SystemExit(3)])
def test_stdio_failure_does_not_report_completion(
    durable_log: Path, error: BaseException
) -> None:
    """Crashes and abnormal exits propagate without a false success record."""
    with patch.object(McpServerOptions, "_observed_server") as observed:
        observed.return_value.run.side_effect = error
        result = CliRunner().invoke(app, ["mcp", "--user", "private-user"])
    assert result.exit_code != 0
    assert result.exception is error
    assert "shutdown outcome=completed" not in durable_log.read_text()


def test_http_forwarder_copies_only_fixed_events(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="biff.server.transport_runtime")
    handler = McpLifecycleHandler()
    for msg in ("Shutting down", "Finished server process [%d]", "private-repo %s", ""):
        handler.emit(
            logging.LogRecord(
                "uvicorn.error",
                logging.INFO,
                "/private/path",
                1,
                msg,
                ("private-payload",),
                None,
            )
        )
    assert caplog.messages == [
        "HTTP MCP shutdown outcome=started",
        "HTTP MCP shutdown outcome=completed",
    ]
    assert all(r.levelno == logging.INFO for r in caplog.records)
    assert "private" not in caplog.text


async def test_uvicorn_graceful_shutdown_is_durable(
    durable_log: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = uvicorn.Config(
        FastMCP("private-repo").http_app(),
        log_config=McpLifecycleHandler.configuration(),
        timeout_graceful_shutdown=1,
        ws="websockets-sansio",
    )
    logging.getLogger().addHandler(caplog.handler)
    caplog.set_level(logging.INFO)
    server = uvicorn.Server(config)
    server.servers = []

    async def terminate() -> None:
        server.handle_exit(signal.SIGTERM, None)

    with (
        patch.object(server, "startup", new=AsyncMock()),
        patch.object(server, "main_loop", new=terminate),
        patch("uvicorn.lifespan.on.LifespanOn.shutdown", new=AsyncMock()),
    ):
        await server._serve()
    assert server.should_exit
    messages = [
        r.message for r in caplog.records if r.name == "biff.server.transport_runtime"
    ]
    assert messages == [
        "HTTP MCP shutdown outcome=started",
        "HTTP MCP shutdown outcome=completed",
    ]
    text = durable_log.read_text()
    assert text.count("HTTP MCP shutdown outcome=started") == 1
    assert text.count("HTTP MCP shutdown outcome=completed") == 1
    assert "private" not in text


async def test_relay_error_episodes_rearm_after_recovery_and_terminal_close(
    caplog: pytest.LogCaptureFixture,
) -> None:
    relay = NatsRelay(url="nats://localhost:4222", repo_name="private-repo")
    callbacks = relay._connection_callbacks(relay._generation)
    caplog.set_level(logging.DEBUG, logger="biff.nats_relay")
    await callbacks["reconnected_cb"]()
    for _ in range(3):
        await callbacks["error_cb"](
            ConnectionRefusedError("private-login /private/path")
        )
    await callbacks["reconnected_cb"]()
    await callbacks["reconnected_cb"]()
    await callbacks["error_cb"](TimeoutError("private-payload"))
    await callbacks["closed_cb"]()
    await callbacks["closed_cb"]()
    await callbacks["error_cb"](ConnectionRefusedError("private-error"))
    errors = [r for r in caplog.records if "NATS error" in r.message]
    assert [r.levelno for r in errors] == [
        logging.INFO,
        logging.DEBUG,
        logging.DEBUG,
        logging.INFO,
        logging.INFO,
    ]
    outcomes = [r.message for r in caplog.records if "outcome=" in r.message]
    assert outcomes == [
        "NATS connection outcome=recovered failures=3 error=ConnectionRefusedError",
        "NATS connection outcome=closed failures=1 error=TimeoutError",
    ]
    assert "private" not in caplog.text
    assert all(r.exc_info is None for r in caplog.records)


async def test_initial_dial_reports_recovery_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    relay = NatsRelay(url="nats://localhost:4222", repo_name="private-repo")
    callbacks = relay._connection_callbacks(relay._generation)
    caplog.set_level(logging.INFO, logger="biff.nats_relay")
    await callbacks["error_cb"](ConnectionRefusedError("private-error"))
    with patch("nats.aio.client.Client.connect", new=AsyncMock()):
        await relay._dial()
        await relay._dial()
    assert caplog.messages == [
        "NATS error (transient, auto-recovering): ConnectionRefusedError",
        "NATS connection outcome=recovered failures=1 error=ConnectionRefusedError",
    ]


@pytest.mark.parametrize("response", ["ok", "missing", "failed", "stale"])
async def test_tracked_traffic_rearms_error_episode_only_for_current_liveness(
    response: str, caplog: pytest.LogCaptureFixture
) -> None:
    relay = NatsRelay(url="nats://localhost:4222", repo_name="private-repo")
    relay._nc = MagicMock()
    callbacks = relay._connection_callbacks(relay._generation)
    caplog.set_level(logging.DEBUG, logger="biff.nats_relay")
    for _ in range(2):
        await callbacks["error_cb"](TimeoutError("private-error"))

    async def traffic() -> str:
        if response == "stale":
            relay._nc = MagicMock()
        if response == "missing":
            raise KeyNotFoundError
        if response == "failed":
            raise RuntimeError("private-error")
        return "private-payload"

    if response in {"missing", "failed"}:
        error = KeyNotFoundError if response == "missing" else RuntimeError
        with pytest.raises(error):
            await relay._tracked("kv.get", traffic())
    else:
        assert await relay._tracked("kv.get", traffic()) == "private-payload"
    recovered = response in {"ok", "missing"}
    outcomes = [r.message for r in caplog.records if "outcome=" in r.message]
    assert outcomes == (
        ["NATS connection outcome=recovered failures=2 error=TimeoutError"]
        if recovered
        else []
    )
    await callbacks["error_cb"](TimeoutError("private-new-error"))
    errors = [r for r in caplog.records if "NATS error" in r.message]
    assert [r.levelno for r in errors] == [
        logging.INFO,
        logging.DEBUG,
        logging.INFO if recovered else logging.DEBUG,
    ]
    assert "private" not in caplog.text
    assert all(r.exc_info is None for r in caplog.records)
