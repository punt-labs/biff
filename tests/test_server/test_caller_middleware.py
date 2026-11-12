"""CallerIdentityMiddleware strips, validates, refuses, and binds ``_caller``."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from biff.caller_identity import CallerIdentity
from biff.server.caller_middleware import CallerIdentityMiddleware

GOOD = {"session_id": "sess-1", "agent_id": "agent-1"}


class _Binder:
    def __init__(self) -> None:
        self.bound: list[CallerIdentity] = []

    @asynccontextmanager
    async def __call__(self, caller: CallerIdentity) -> AsyncGenerator[None]:
        self.bound.append(caller)
        yield


def _server(binder: _Binder) -> tuple[FastMCP, list[dict[str, object]]]:
    mcp = FastMCP("t")
    seen: list[dict[str, object]] = []

    @mcp.tool
    def echo(message: str) -> str:
        seen.append({"message": message})
        return message

    mcp.add_middleware(CallerIdentityMiddleware(binder))
    return mcp, seen


async def test_valid_caller_is_stripped_and_bound() -> None:
    binder = _Binder()
    mcp, seen = _server(binder)
    async with Client(mcp) as client:
        result = await client.call_tool("echo", {"message": "hi", "_caller": GOOD})
    assert not result.is_error
    assert seen == [{"message": "hi"}]
    assert binder.bound == [CallerIdentity("sess-1", "agent-1")]


async def test_unidentified_call_is_refused_naming_causes() -> None:
    binder = _Binder()
    mcp, seen = _server(binder)
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match=r"stamping hook.*not installed"):
            await client.call_tool("echo", {"message": "hi"})
    assert seen == []
    assert binder.bound == []


@pytest.mark.parametrize(
    "bad",
    [
        "string",
        {"session_id": "s"},
        {"session_id": "../x", "agent_id": "a"},
        {"session_id": "s", "agent_id": ""},
    ],
)
async def test_malformed_caller_is_named_error(bad: object) -> None:
    binder = _Binder()
    mcp, seen = _server(binder)
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="_caller"):
            await client.call_tool("echo", {"message": "hi", "_caller": bad})
    assert seen == []
    assert binder.bound == []


async def test_create_server_strips_and_refuses_on_a_real_tool(tmp_path: Path) -> None:
    from biff.models import BiffConfig
    from biff.server.app import create_server
    from biff.server.state import create_state
    from biff.testing.caller import StampedClient

    state = create_state(
        BiffConfig(user="rmh", repo_name="test"), tmp_path, dormant=True
    )
    async with StampedClient(create_server(state)) as client:
        ok = await client.call_tool("who")
        assert not ok.is_error
    async with StampedClient(create_server(state), caller=None) as client:
        with pytest.raises(ToolError, match="stamping hook"):
            await client.call_tool("who")
