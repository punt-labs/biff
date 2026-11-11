"""Prove fastmcp 3 middleware can strip an undeclared argument before validation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp import Client, FastMCP
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

if TYPE_CHECKING:
    from fastmcp.tools import ToolResult
    from mcp.types import CallToolRequestParams


class _Strip(Middleware):
    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        args = context.message.arguments
        if args is not None:
            args.pop("_caller", None)
        return await call_next(context)


async def test_middleware_strip_precedes_schema_validation() -> None:
    mcp = FastMCP("probe")
    seen: list[dict[str, object]] = []

    @mcp.tool
    def echo(message: str) -> str:
        seen.append({"message": message})
        return message

    mcp.add_middleware(_Strip())
    async with Client(mcp) as client:
        result = await client.call_tool(
            "echo", {"message": "hi", "_caller": {"session_id": "s", "agent_id": "a"}}
        )
    assert not result.is_error
    assert seen == [{"message": "hi"}]


async def test_without_strip_schema_rejects_undeclared_argument() -> None:
    mcp = FastMCP("probe-control")

    @mcp.tool
    def echo(message: str) -> str:
        return message

    async with Client(mcp) as client:
        result = await client.call_tool(
            "echo",
            {"message": "hi", "_caller": {"session_id": "s", "agent_id": "a"}},
            raise_on_error=False,
        )
    assert result.is_error
