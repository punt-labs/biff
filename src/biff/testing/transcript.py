"""Transcript capture and rendering for biff integration tests.

Records MCP tool calls and responses during test runs, then renders
them as human-readable demo output showing biff in action.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mcp.types import TextContent

if TYPE_CHECKING:
    from fastmcp import Client


@dataclass(frozen=True)
class TranscriptEntry:
    """A single tool call and its response."""

    tool: str
    arguments: dict[str, object]
    result: str
    is_error: bool = False
    user: str = ""


def _format_command(entry: TranscriptEntry) -> str:
    """Format a transcript entry as a terminal-style command."""
    tool = entry.tool
    args = entry.arguments
    prefix = f"@{entry.user} " if entry.user else ""

    # Map tool names to their slash-command syntax
    if tool == "plan" and "message" in args:
        return f'{prefix}> /plan "{args["message"]}"'
    if tool == "finger" and "user" in args:
        user = args["user"]
        at_user = user if str(user).startswith("@") else f"@{user}"
        return f"{prefix}> /finger {at_user}"
    if tool == "mesg" and "enabled" in args:
        state = "on" if args["enabled"] else "off"
        return f"{prefix}> /mesg {state}"
    if tool == "write" and "to" in args and "message" in args:
        to = args["to"]
        at_to = to if str(to).startswith("@") else f"@{to}"
        return f'{prefix}> /write {at_to} "{args["message"]}"'
    if tool == "read_messages":
        return f"{prefix}> /read"
    if tool == "who":
        return f"{prefix}> /who"

    # Fallback: generic tool call
    arg_str = " ".join(f"{k}={v!r}" for k, v in args.items())
    return f"{prefix}> /{tool} {arg_str}".rstrip()


@dataclass
class Transcript:
    """Captured sequence of tool calls and responses."""

    title: str
    description: str = ""
    entries: list[TranscriptEntry] = field(default_factory=list[TranscriptEntry])

    def add(
        self,
        tool: str,
        arguments: dict[str, object],
        result: str,
        *,
        is_error: bool = False,
        user: str = "",
    ) -> None:
        """Record a tool call and its response."""
        self.entries.append(
            TranscriptEntry(
                tool=tool,
                arguments=arguments,
                result=result,
                is_error=is_error,
                user=user,
            )
        )

    def render(self) -> str:
        """Render the transcript as human-readable demo output."""
        lines: list[str] = [f"# {self.title}"]
        if self.description:
            lines.append(f"# {self.description}")
        lines.append("")

        for entry in self.entries:
            lines.append(_format_command(entry))
            if entry.is_error:
                lines.append(f"ERROR: {entry.result}")
            else:
                lines.append(entry.result)
            lines.append("")

        return "\n".join(lines)


@dataclass
class RecordingClient:
    """Wraps a FastMCP Client to record tool calls into a Transcript."""

    client: Client[Any]
    transcript: Transcript
    user: str = ""

    async def call(self, tool_name: str, **kwargs: object) -> str:
        """Call a tool and record the interaction in the transcript."""
        result = await self.client.call_tool(tool_name, kwargs)
        is_error = bool(result.is_error)
        text_parts = [
            block.text for block in result.content if isinstance(block, TextContent)
        ]
        text = "\n".join(text_parts) if text_parts else "(no output)"
        self.transcript.add(
            tool_name, dict(kwargs), text, is_error=is_error, user=self.user
        )
        return text
