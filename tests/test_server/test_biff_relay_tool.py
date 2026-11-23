"""Tests for the ``biff_relay`` MCP tool: it writes an explicit relay type."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastmcp.tools.function_tool import FunctionTool

from biff.config import resolve_relay_config
from biff.models import BiffConfig
from biff.server.app import create_server
from tests._state import create_state


async def _tool(tmp_path: Path):
    state = create_state(
        BiffConfig(user="kai", repo_name="_test-relay-tool"),
        tmp_path,
        tty="tty1",
        hostname="test-host",
        pwd="/test",
        repo_root=tmp_path,
    )
    tool = await create_server(state).get_tool("biff_relay")
    assert isinstance(tool, FunctionTool)
    return tool.fn


def _written(tmp_path: Path, name: str = "config.yaml") -> object:
    path = tmp_path / ".punt-labs" / "biff" / name
    return yaml.safe_load(path.read_text())


@pytest.fixture(autouse=True)
def _git_repo(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()


class TestBiffRelayTool:
    async def test_url_writes_an_explicit_nats_type(self, tmp_path: Path) -> None:
        fn = await _tool(tmp_path)
        await fn(url="tls://relay.example:4222")
        assert _written(tmp_path) == {
            "relay": {"type": "nats", "url": "tls://relay.example:4222"}
        }

    async def test_existing_arguments_still_work(self, tmp_path: Path) -> None:
        fn = await _tool(tmp_path)
        await fn("nats://host:4222", "/creds", True)
        assert _written(tmp_path, "config.local.yaml") == {
            "relay": {
                "type": "nats",
                "url": "nats://host:4222",
                "auth": {"credentials": "/creds"},
            }
        }

    async def test_choose_local(self, tmp_path: Path) -> None:
        fn = await _tool(tmp_path)
        result = await fn(relay_type="local")
        assert _written(tmp_path) == {"relay": {"type": "local"}}
        assert "local" in result
        assert resolve_relay_config(tmp_path) == (None, None, False)

    async def test_choose_demo(self, tmp_path: Path) -> None:
        fn = await _tool(tmp_path)
        await fn(relay_type="demo", local=True)
        assert _written(tmp_path, "config.local.yaml") == {"relay": {"type": "demo"}}
        assert resolve_relay_config(tmp_path)[0] == "tls://connect.ngs.global"

    @pytest.mark.parametrize("kind", ["local", "demo"])
    async def test_local_and_demo_take_no_url_or_auth(
        self, tmp_path: Path, kind: str
    ) -> None:
        fn = await _tool(tmp_path)
        assert "Error" in await fn(url="nats://x", relay_type=kind)
        assert "Error" in await fn(auth="/creds", local=True, relay_type=kind)
        assert not (tmp_path / ".punt-labs" / "biff" / "config.yaml").exists()

    async def test_nats_needs_a_url(self, tmp_path: Path) -> None:
        fn = await _tool(tmp_path)
        assert "Error" in await fn(relay_type="nats")

    async def test_no_arguments_is_an_error(self, tmp_path: Path) -> None:
        fn = await _tool(tmp_path)
        assert "Error" in await fn()

    async def test_unknown_type_is_an_error(self, tmp_path: Path) -> None:
        fn = await _tool(tmp_path)
        assert "Error" in await fn(relay_type="docker")

    async def test_bad_scheme_is_still_rejected(self, tmp_path: Path) -> None:
        fn = await _tool(tmp_path)
        assert "invalid relay URL scheme" in await fn(url="http://x")
