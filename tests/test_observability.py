"""Exercise Biff's shared diagnostics and telemetry boundaries."""

from __future__ import annotations

import ast
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from typer.testing import CliRunner

from biff.__main__ import app
from biff.models import BiffConfig
from biff.server.app import create_server
from biff.server.state import create_state
from biff.testing.caller import StampedClient
from biff.usage import McpUsageMiddleware, UsageCapture


@pytest.fixture
def usage_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate diagnostic and usage files and enable default local capture."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("PUNT_TELEMETRY", "BIFF_TELEMETRY", "PUNT_TELEMETRY_EXPORTER"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / ".punt-labs" / "biff"


def _spans(database: Path) -> list[tuple[str, int, str]]:
    with closing(sqlite3.connect(database)) as conn:
        return [
            (str(name), int(version), str(attributes))
            for name, version, attributes in conn.execute(
                "SELECT name, schema_version, attributes FROM spans ORDER BY start_ns"
            )
        ]


def test_cli_writes_private_usage_separate_from_diagnostics(usage_home: Path) -> None:
    result = CliRunner().invoke(app, ["--user", "private-identity", "version"])
    assert result.exit_code == 0, result.output

    database = usage_home / "telemetry.db"
    assert database.stat().st_mode & 0o777 == 0o600
    assert usage_home.stat().st_mode & 0o777 == 0o700
    assert usage_home.joinpath("logs", "biff.log").is_file()
    [(name, version, attrs)] = _spans(database)
    assert (name, version) == ("biff.cli.invoke", 1)
    assert '"command": "version"' in attrs
    assert "private-identity" not in attrs


@pytest.mark.parametrize(
    "switch", ["PUNT_TELEMETRY", "BIFF_TELEMETRY", "--no-telemetry"]
)
def test_any_off_switch_avoids_store(
    usage_home: Path, monkeypatch: pytest.MonkeyPatch, switch: str
) -> None:
    args = ["version"]
    if switch.startswith("--"):
        args.insert(0, switch)
    else:
        monkeypatch.setenv(switch, "off")
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert not usage_home.joinpath("telemetry.db").exists()
    assert usage_home.joinpath("logs", "biff.log").is_file()


def test_unusable_telemetry_database_does_not_fail_cli(usage_home: Path) -> None:
    usage_home.joinpath("telemetry.db").mkdir(parents=True)
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0, result.output


async def test_mcp_records_outcomes_without_protocol_content(usage_home: Path) -> None:
    state = create_state(
        BiffConfig(user="rmh", repo_name="test"), usage_home, dormant=True
    )
    capture = UsageCapture.open(enabled=True)
    usage = capture.telemetry
    server = create_server(state)
    server.add_middleware(McpUsageMiddleware(capture))
    try:
        async with StampedClient(server) as client:
            assert not (await client.call_tool("who")).is_error
            with pytest.raises(ToolError):
                await client.call_tool("finger", {"private-argument": "rejected-input"})
        usage.event("biff.cli.invoke", 1, command="private-content")
        usage.event("biff.cli.invoke", 1, command="who", message="private-content")
        usage.event("biff.cli.invoke", 1, command=123)
        usage.event("biff.cli.invoke", 2, command="who")
    finally:
        usage.shutdown()

    rows = _spans(usage_home / "telemetry.db")
    assert [(name, version) for name, version, _ in rows] == [("biff.mcp.call", 1)] * 2
    assert '"outcome": "ok"' in rows[0][2]
    assert '"outcome": "error"' in rows[1][2]
    assert '"tool": "who"' in rows[0][2]
    assert '"tool": "finger"' in rows[1][2]
    for _, _, attributes in rows:
        assert all(
            value not in attributes
            for value in ("private-argument", "rejected-input", "stamping hook")
        )


def test_source_has_no_string_configured_unsafe_logging_apis() -> None:
    banned = {
        "logging.basicConfig",
        "logging.handlers.RotatingFileHandler",
        "logging.handlers.TimedRotatingFileHandler",
    }
    source = Path(__file__).resolve().parents[1] / "src"
    for path in source.rglob("*.py"):
        tree = ast.parse(path.read_bytes(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in banned, f"{path}:{node.lineno}: {node.value}"
