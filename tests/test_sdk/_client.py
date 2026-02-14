"""SDK test harness for driving Claude Code sessions with biff.

Provides ``SDKClient`` — sends natural-language prompts to a Claude Code
session with biff configured as an MCP server.  Claude decides which tools
to call; the client captures the full message flow and records tool
interactions into the shared ``Transcript``.

Usage::

    async def test_plan_via_sdk(kai: SDKClient, eric: SDKClient) -> None:
        kai.transcript.title = "SDK: plan visible via finger"
        await kai.prompt('Call the "plan" tool with message "writing tests".')
        result = await eric.prompt('Call the "finger" tool with user "@kai".')
        assert "writing tests" in result.tool_output
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from biff.testing import Transcript

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions

_MCP_TOOL_PREFIX = "mcp__biff__"


# ---------------------------------------------------------------------------
# Structured output types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    """A single tool invocation observed in the SDK message stream."""

    name: str
    arguments: dict[str, object]


@dataclass(frozen=True)
class SDKResult:
    """Structured output from an SDK query.

    Attributes:
        tool_calls: Tools Claude chose to invoke (prefix-stripped names).
        tool_output: Concatenated raw text output from biff tools.
        result_text: Claude's final response text.
        is_error: Whether the session ended in error.
        cost_usd: Total API cost for this query.
    """

    tool_calls: tuple[ToolCall, ...]
    tool_output: str
    result_text: str
    is_error: bool
    cost_usd: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_options(
    user: str,
    data_dir: Path,
    *,
    max_turns: int = 2,
) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions with biff as an MCP server."""
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
        mcp_servers={
            "biff": {
                "type": "stdio",
                "command": "uv",
                "args": [
                    "run",
                    "biff",
                    "serve",
                    "--user",
                    user,
                    "--data-dir",
                    str(data_dir),
                    "--transport",
                    "stdio",
                ],
            },
        },
        allowed_tools=["mcp__biff__*"],
        permission_mode="bypassPermissions",
        max_turns=max_turns,
        max_budget_usd=0.05,
        env={"CLAUDECODE": ""},
        system_prompt=(
            "You are a test harness. When asked to use a biff tool, "
            "call it exactly as instructed. Do not explain, just act."
        ),
    )


def _extract_tool_result_text(content: Any) -> str:
    """Parse human-readable text from a ToolResultBlock's content.

    Biff tools return TextContent which FastMCP serializes as
    ``{"result": "..."}`` over the wire.  Fall back to raw string.
    """
    if not isinstance(content, str):
        return ""
    try:
        parsed: dict[str, object] = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content
    if "result" in parsed:
        val = parsed["result"]
        return str(val)
    return content


def _strip_prefix(tool_name: str) -> str:
    """Strip ``mcp__biff__`` prefix from tool names."""
    if tool_name.startswith(_MCP_TOOL_PREFIX):
        return tool_name[len(_MCP_TOOL_PREFIX) :]
    return tool_name


def _extract_calls(messages: list[Any]) -> list[ToolCall]:
    """Extract tool calls from AssistantMessages."""
    from claude_agent_sdk import AssistantMessage, ToolUseBlock

    return [
        ToolCall(
            name=_strip_prefix(block.name),
            arguments=dict(block.input),
        )
        for msg in messages
        if isinstance(msg, AssistantMessage)
        for block in msg.content
        if isinstance(block, ToolUseBlock)
    ]


def _extract_outputs(messages: list[Any]) -> list[str]:
    """Extract raw tool output texts from UserMessages."""
    from claude_agent_sdk import ToolResultBlock, UserMessage

    return [
        _extract_tool_result_text(block.content)
        for msg in messages
        if isinstance(msg, UserMessage)
        for block in msg.content
        if isinstance(block, ToolResultBlock)
    ]


def _extract_result(messages: list[Any]) -> tuple[str, bool, float]:
    """Extract final result text, error flag, and cost from ResultMessage."""
    from claude_agent_sdk import ResultMessage

    for msg in messages:
        if isinstance(msg, ResultMessage):
            return (
                msg.result or "",
                msg.is_error,
                msg.total_cost_usd or 0.0,
            )
    return ("", False, 0.0)


# ---------------------------------------------------------------------------
# SDKClient
# ---------------------------------------------------------------------------


@dataclass
class SDKClient:
    """Drives Claude Code sessions with biff configured as an MCP server.

    Each ``prompt()`` call spawns a new Claude session, captures the full
    message flow, records tool interactions into the shared ``Transcript``,
    and returns a structured ``SDKResult``.
    """

    user: str
    data_dir: Path
    transcript: Transcript
    max_turns: int = 2

    async def prompt(self, text: str) -> SDKResult:
        """Send a prompt to Claude; return structured results.

        Claude decides which biff tools to call.  All tool interactions
        are automatically recorded in the transcript.
        """
        from claude_agent_sdk import query

        options = _make_options(self.user, self.data_dir, max_turns=self.max_turns)
        messages: list[Any] = [msg async for msg in query(prompt=text, options=options)]

        tool_calls = _extract_calls(messages)
        tool_outputs = _extract_outputs(messages)
        result_text, is_error, cost_usd = _extract_result(messages)

        for i, tc in enumerate(tool_calls):
            output = tool_outputs[i] if i < len(tool_outputs) else ""
            self.transcript.add(
                tc.name,
                dict(tc.arguments),
                output,
                user=self.user,
            )

        return SDKResult(
            tool_calls=tuple(tool_calls),
            tool_output="\n".join(tool_outputs),
            result_text=result_text,
            is_error=is_error,
            cost_usd=cost_usd,
        )
