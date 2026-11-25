"""ClientSummary reads who connected from an initialize request."""

from __future__ import annotations

import pytest

from biff.server.client_summary import ClientSummary


def test_a_full_request_names_the_client_and_its_capabilities() -> None:
    summary = ClientSummary.from_initialize(
        {
            "clientInfo": {"name": "claude-code", "version": "2.1.330"},
            "capabilities": {"experimental": {"zeta": {}, "claude/channel": {}}},
        }
    )
    assert str(summary) == (
        "client=claude-code/2.1.330 experimental=claude/channel,zeta"
    )


@pytest.mark.parametrize(
    "params",
    [
        None,
        {},
        {"clientInfo": "claude-code", "capabilities": []},
        {"clientInfo": {"name": 7, "version": ""}, "capabilities": {"experimental": 1}},
    ],
)
def test_missing_or_malformed_fields_read_as_unknown(
    params: dict[str, object] | None,
) -> None:
    summary = ClientSummary.from_initialize(params)
    assert str(summary) == "client=unknown/unknown experimental=none"


def test_the_handshake_line_carries_the_version_and_the_client() -> None:
    summary = ClientSummary("claude-code", "2.1.330", ("claude/channel",))
    assert summary.handshake("2025-11-25") == (
        "MCP handshake: protocolVersion=2025-11-25 channel_mode=on"
        " client=claude-code/2.1.330 experimental=claude/channel"
    )
