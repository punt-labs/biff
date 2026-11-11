"""Test clients declare a realistic main-thread ``_caller`` by default."""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.client.transports import FastMCPTransport

from biff.caller_identity import CallerIdentity
from biff.testing.caller import DEFAULT_CALLER, StampedClient


def _server() -> tuple[FastMCP, list[dict[str, object]]]:
    mcp = FastMCP("t")
    seen: list[dict[str, object]] = []

    @mcp.tool
    def echo(message: str, _caller: dict[str, str] | None = None) -> str:
        seen.append({"_caller": _caller})
        return message

    return mcp, seen


def test_default_caller_is_a_valid_main_thread_identity() -> None:
    parsed = CallerIdentity.from_wire(DEFAULT_CALLER)
    assert (parsed.session_id, parsed.agent_id) == ("test-session", "4242")


async def test_call_tool_stamps_the_default_caller() -> None:
    mcp, seen = _server()
    async with StampedClient(FastMCPTransport(mcp)) as client:
        await client.call_tool("echo", {"message": "hi"})
    assert seen == [{"_caller": DEFAULT_CALLER}]


async def test_a_test_can_override_the_caller() -> None:
    mcp, seen = _server()
    other = {"session_id": "other", "agent_id": "sub-1"}
    async with StampedClient(FastMCPTransport(mcp), caller=other) as client:
        await client.call_tool("echo", {"message": "hi"})
    assert seen == [{"_caller": other}]


async def test_a_test_can_omit_the_caller() -> None:
    mcp, seen = _server()
    async with StampedClient(FastMCPTransport(mcp), caller=None) as client:
        await client.call_tool("echo", {"message": "hi"})
    assert seen == [{"_caller": None}]


async def test_an_explicit_caller_argument_is_not_replaced() -> None:
    mcp, seen = _server()
    mine = {"session_id": "mine", "agent_id": "mine"}
    async with StampedClient(FastMCPTransport(mcp)) as client:
        await client.call_tool("echo", {"message": "hi", "_caller": mine})
    assert seen == [{"_caller": mine}]


async def test_recording_client_stamps_the_default_caller_on_a_plain_client() -> None:
    from fastmcp import Client

    from biff.testing.transcript import RecordingClient, Transcript

    mcp, seen = _server()
    async with Client(FastMCPTransport(mcp)) as raw:
        text = await RecordingClient(client=raw, transcript=Transcript(title="t")).call(
            "echo", message="hi"
        )
    assert text == "hi"
    assert seen == [{"_caller": DEFAULT_CALLER}]


async def test_recording_client_can_override_or_omit_the_caller() -> None:
    from fastmcp import Client

    from biff.testing.transcript import RecordingClient, Transcript

    mcp, seen = _server()
    other = {"session_id": "other", "agent_id": "sub-1"}
    async with Client(FastMCPTransport(mcp)) as raw:
        rec = RecordingClient(client=raw, transcript=Transcript(title="t"))
        await rec.call("echo", message="a", _caller=other)
        await rec.call("echo", message="b", _caller=None)
    assert seen == [{"_caller": other}, {"_caller": None}]
