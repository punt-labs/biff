"""CallerIdentityMiddleware strips, validates, refuses, and binds ``_caller``."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from biff.caller_identity import CallerIdentity
from biff.server.caller_middleware import CallerIdentityMiddleware
from biff.server.caller_policy import CallerPolicy

GOOD = {"session_id": "sess-1", "agent_id": "agent-1"}


class _Binder:
    def __init__(self) -> None:
        self.bound: list[CallerIdentity] = []

    @asynccontextmanager
    async def __call__(self, caller: CallerIdentity) -> AsyncGenerator[None]:
        self.bound.append(caller)
        yield


def _server(
    binder: _Binder, default_caller: CallerIdentity | None = None
) -> tuple[FastMCP, list[dict[str, object]]]:
    mcp = FastMCP("t")
    seen: list[dict[str, object]] = []

    @mcp.tool
    def echo(message: str) -> str:
        seen.append({"message": message})
        return message

    mcp.add_middleware(CallerIdentityMiddleware(CallerPolicy(binder, default_caller)))
    return mcp, seen


async def test_valid_caller_is_stripped_and_bound() -> None:
    binder = _Binder()
    mcp, seen = _server(binder)
    async with Client(mcp) as client:
        result = await client.call_tool("echo", {"message": "hi", "_caller": GOOD})
    assert not result.is_error
    assert seen == [{"message": "hi"}]
    assert binder.bound == [CallerIdentity("sess-1", "agent-1")]


async def test_default_caller_binds_only_unstamped_calls() -> None:
    binder = _Binder()
    default = CallerIdentity("startup-session", "devin")
    mcp, seen = _server(binder, default)
    async with Client(mcp) as client:
        await client.call_tool("echo", {"message": "unstamped"})
        await client.call_tool("echo", {"message": "stamped", "_caller": GOOD})
    assert seen == [{"message": "unstamped"}, {"message": "stamped"}]
    assert binder.bound == [default, CallerIdentity("sess-1", "agent-1")]


@pytest.mark.parametrize("bad", [None, {}, "", {"session_id": "s", "agent_id": "../x"}])
async def test_default_caller_does_not_replace_malformed_identity(bad: object) -> None:
    binder = _Binder()
    mcp, seen = _server(binder, CallerIdentity("startup-session", "devin"))
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="_caller"):
            await client.call_tool("echo", {"message": "refused", "_caller": bad})
    assert seen == []
    assert binder.bound == []


async def test_unidentified_call_is_refused_naming_causes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    binder = _Binder()
    mcp, seen = _server(binder)
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match=r"stamping hook.*not installed"):
            await client.call_tool("echo", {"message": "hi"})
    assert seen == []
    assert binder.bound == []
    assert (
        "biff.server.caller_policy",
        logging.WARNING,
        "Tool call denied: missing caller identity",
    ) in caplog.record_tuples


@pytest.mark.parametrize(
    "bad",
    [
        "string",
        {"session_id": "s"},
        {"session_id": "../x", "agent_id": "a"},
        {"session_id": "s", "agent_id": ""},
    ],
)
async def test_malformed_caller_is_named_error(
    bad: object, caplog: pytest.LogCaptureFixture
) -> None:
    binder = _Binder()
    mcp, seen = _server(binder)
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="_caller"):
            await client.call_tool("echo", {"message": "hi", "_caller": bad})
    assert seen == []
    assert binder.bound == []
    assert (
        "biff.server.caller_policy",
        logging.WARNING,
        "Tool call denied: malformed caller identity",
    ) in caplog.record_tuples
    records = [r for r in caplog.records if r.name == "biff.server.caller_policy"]
    assert len(records) == 1
    assert records[0].args == ()


async def test_create_server_strips_and_refuses_on_a_real_tool(tmp_path: Path) -> None:
    from biff.models import BiffConfig
    from biff.server.app import create_server
    from biff.testing.caller import StampedClient
    from tests._state import create_state

    state = create_state(
        BiffConfig(user="rmh", repo_name="test"), tmp_path, dormant=True
    )
    async with StampedClient(create_server(state)) as client:
        ok = await client.call_tool("who")
        assert not ok.is_error
    async with StampedClient(create_server(state), caller=None) as client:
        with pytest.raises(ToolError, match="stamping hook"):
            await client.call_tool("who")
