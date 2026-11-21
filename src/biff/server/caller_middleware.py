"""Strip, validate, and bind the harness-declared ``_caller`` on every tool call."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from biff.server.caller_policy import CallerPolicy

if TYPE_CHECKING:
    from fastmcp.tools import ToolResult
    from mcp.types import CallToolRequestParams


class CallerIdentityMiddleware(Middleware):
    """Remove ``_caller`` before schema validation, then run the call inside *bind*.

    fastmcp 3 runs ``on_call_tool`` before the tool's argument schema is
    checked, so the undeclared ``_caller`` is popped here and never reaches
    a tool. Missing identity is refused unless the process explicitly declares
    a default caller at launch. Malformed identity is always refused.
    """

    def __init__(self, policy: CallerPolicy) -> None:
        self._policy = policy

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Validate caller identity and dispatch the tool within its session.

        Raises:
            ToolError: The caller identity is missing or malformed, or the tool fails.
        """
        caller = self._policy.resolve(context.message.arguments)
        async with self._policy.bind(caller):
            return await call_next(context)
