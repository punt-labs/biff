"""Tests for poll_config MCP tools — set_poll_interval / get_poll_status."""

from __future__ import annotations

from pathlib import Path

from fastmcp.tools.function_tool import FunctionTool

from biff.models import BiffConfig
from biff.server.app import create_server
from biff.server.state import ServerState, create_state

_TEST_REPO = "_test-server"


def _make_state(tmp_path: Path, *, poll_interval: float = 2.0) -> ServerState:
    config = BiffConfig(user="kai", repo_name=_TEST_REPO, poll_interval=poll_interval)
    return create_state(
        config,
        tmp_path,
        tty="tty1",
        hostname="test-host",
        pwd="/test",
        repo_root=tmp_path,
    )


async def _get_tool_fn(state: ServerState, tool_name: str):
    mcp = create_server(state)
    tool = await mcp.get_tool(tool_name)
    assert tool is not None
    assert isinstance(tool, FunctionTool)
    return tool.fn


class TestSetPollInterval:
    async def test_set_valid_seconds(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        fn = await _get_tool_fn(state, "set_poll_interval")
        result = await fn(interval="5s")
        assert "5s" in result
        assert "Restart" in result

    async def test_set_valid_minutes(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        fn = await _get_tool_fn(state, "set_poll_interval")
        result = await fn(interval="2m")
        assert "2m" in result
        assert "Restart" in result

    async def test_disable(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        fn = await _get_tool_fn(state, "set_poll_interval")
        result = await fn(interval="n")
        assert "disabled" in result.lower()
        assert "Restart" in result

    async def test_invalid_interval(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        fn = await _get_tool_fn(state, "set_poll_interval")
        result = await fn(interval="banana")
        assert "Invalid" in result

    async def test_persists_to_local_yaml(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        # Create the config dir so write succeeds
        config_dir = tmp_path / ".punt-labs" / "biff"
        config_dir.mkdir(parents=True)
        fn = await _get_tool_fn(state, "set_poll_interval")
        await fn(interval="10s")
        import yaml

        local_yaml = config_dir / "config.local.yaml"
        assert local_yaml.exists()
        data = yaml.safe_load(local_yaml.read_text())
        assert data["poll_interval"] == 10.0


class TestGetPollStatus:
    async def test_default_interval(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        fn = await _get_tool_fn(state, "get_poll_status")
        result = await fn()
        assert "active" in result.lower()
        assert "2s" in result

    async def test_disabled(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path, poll_interval=0.0)
        fn = await _get_tool_fn(state, "get_poll_status")
        result = await fn()
        assert "disabled" in result.lower()

    async def test_minutes_display(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path, poll_interval=120.0)
        fn = await _get_tool_fn(state, "get_poll_status")
        result = await fn()
        assert "2m" in result


class TestParseInterval:
    """Unit tests for _parse_interval helper."""

    def test_seconds(self) -> None:
        from biff.server.tools.poll_config import _parse_interval

        assert _parse_interval("2s") == 2.0
        assert _parse_interval("30s") == 30.0

    def test_minutes(self) -> None:
        from biff.server.tools.poll_config import _parse_interval

        assert _parse_interval("1m") == 60.0
        assert _parse_interval("5m") == 300.0

    def test_disable(self) -> None:
        from biff.server.tools.poll_config import _parse_interval

        assert _parse_interval("n") is None

    def test_invalid(self) -> None:
        from biff.server.tools.poll_config import _parse_interval

        assert _parse_interval("banana") == -1.0
        assert _parse_interval("") == -1.0

    def test_whitespace_stripped(self) -> None:
        from biff.server.tools.poll_config import _parse_interval

        assert _parse_interval("  5s  ") == 5.0
        assert _parse_interval("  N  ") is None
