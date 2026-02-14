"""Proof-of-concept: Claude Agent SDK drives biff via MCP.

Validates that the SDK can:
1. Launch a Claude session with biff as an MCP server
2. Claude discovers and calls biff tools
3. Tool results flow back through the session
4. Cross-user visibility works across two SDK sessions sharing a data dir

SDK message flow:
  SystemMessage  → MCP init
  AssistantMessage(ToolUseBlock)  → Claude calls a tool
  UserMessage(ToolResultBlock)    → raw tool output fed back
  AssistantMessage(TextBlock)     → Claude's summary of the result
  ResultMessage                   → final .result text
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

pytestmark = pytest.mark.sdk


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _skip_without_api_key() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")


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
        # Clear CLAUDECODE to allow nested session from within Claude Code
        env={"CLAUDECODE": ""},
        system_prompt=(
            "You are a test harness. When asked to use a biff tool, "
            "call it exactly as instructed. Do not explain, just act."
        ),
    )


def _categorize(messages: list[Any]) -> dict[str, list[Any]]:
    """Bucket SDK messages by type."""
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        SystemMessage,
        UserMessage,
    )

    buckets: dict[str, list[Any]] = {
        "system": [],
        "assistant": [],
        "user": [],
        "result": [],
        "other": [],
    }
    for msg in messages:
        if isinstance(msg, SystemMessage):
            buckets["system"].append(msg)
        elif isinstance(msg, AssistantMessage):
            buckets["assistant"].append(msg)
        elif isinstance(msg, UserMessage):
            buckets["user"].append(msg)
        elif isinstance(msg, ResultMessage):
            buckets["result"].append(msg)
        else:
            buckets["other"].append(msg)
    return buckets


def _tool_uses(msgs: list[AssistantMessage]) -> list[ToolUseBlock]:
    """Extract ToolUseBlock from assistant messages."""
    from claude_agent_sdk import ToolUseBlock

    return [b for m in msgs for b in m.content if isinstance(b, ToolUseBlock)]


def _tool_results(msgs: list[UserMessage]) -> list[ToolResultBlock]:
    """Extract ToolResultBlock from user messages (tool responses)."""
    from claude_agent_sdk import ToolResultBlock

    return [b for m in msgs for b in m.content if isinstance(b, ToolResultBlock)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSDKCanCallBiffTools:
    """Verify the SDK can drive a Claude session that calls biff tools."""

    async def test_plan_tool_via_sdk(self, shared_data_dir: Path) -> None:
        """Claude sets a plan via the biff MCP server."""
        _skip_without_api_key()
        from claude_agent_sdk import query

        prompt = 'Call the "plan" tool with message "testing SDK integration".'
        options = _make_options("kai", shared_data_dir)

        all_msgs = [msg async for msg in query(prompt=prompt, options=options)]
        cats = _categorize(all_msgs)

        assert cats["assistant"], "Expected assistant messages"

        uses = _tool_uses(cats["assistant"])
        plans = [tu for tu in uses if "plan" in tu.name]
        names = [tu.name for tu in uses]
        assert plans, f"Expected plan tool call, got: {names}"

        assert plans[0].input.get("message") == "testing SDK integration"

        assert cats["result"], "Expected a ResultMessage"
        assert not cats["result"][0].is_error

    async def test_finger_tool_via_sdk(self, shared_data_dir: Path) -> None:
        """Claude checks a user's status via the finger tool."""
        _skip_without_api_key()
        from claude_agent_sdk import query

        # Set kai's plan via direct subprocess (no API call)
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport

        transport = StdioTransport(
            command="uv",
            args=[
                "run",
                "biff",
                "serve",
                "--user",
                "kai",
                "--data-dir",
                str(shared_data_dir),
                "--transport",
                "stdio",
            ],
        )
        async with Client(transport) as client:
            await client.call_tool("plan", {"message": "working on SDK spike"})

        # SDK session: eric fingers kai
        prompt = 'Call the "finger" tool with user "@kai".'
        options = _make_options("eric", shared_data_dir)

        all_msgs = [msg async for msg in query(prompt=prompt, options=options)]
        cats = _categorize(all_msgs)

        # Verify finger tool was called
        uses = _tool_uses(cats["assistant"])
        fingers = [tu for tu in uses if "finger" in tu.name]
        names = [tu.name for tu in uses]
        assert fingers, f"Expected finger call, got: {names}"

        # Verify tool result contains kai's plan (UserMessage has ToolResultBlock)
        results = _tool_results(cats["user"])
        raw_texts = [r.content for r in results if isinstance(r.content, str)]
        assert any("working on SDK spike" in t for t in raw_texts), (
            f"Expected kai's plan in tool results, got: {raw_texts}"
        )


class TestCrossSessionVisibility:
    """Two SDK-driven sessions share state through the filesystem."""

    async def test_plan_visible_across_sessions(self, shared_data_dir: Path) -> None:
        """kai sets plan in one session; eric sees it in another."""
        _skip_without_api_key()
        from claude_agent_sdk import query

        # Session 1: kai sets plan
        kai_prompt = 'Call the "plan" tool with message "cross-session test".'
        kai_opts = _make_options("kai", shared_data_dir)

        async for _ in query(prompt=kai_prompt, options=kai_opts):
            pass

        # Session 2: eric checks who
        eric_prompt = 'Call the "who" tool to see who is online.'
        eric_opts = _make_options("eric", shared_data_dir)

        all_msgs = [msg async for msg in query(prompt=eric_prompt, options=eric_opts)]
        cats = _categorize(all_msgs)

        # Check raw tool result in UserMessage
        results = _tool_results(cats["user"])
        raw_texts = [r.content for r in results if isinstance(r.content, str)]
        assert any("kai" in t and "cross-session" in t for t in raw_texts), (
            f"Expected kai's plan in who results, got: {raw_texts}"
        )
