"""This repo's .envrc must leave the channel-blocking variables unset (DES-063).

The org template exports them in every repo; in biff any one of them silently
turns Claude Code's channels off.  This test stops a template rollout from
quietly undoing the deviation again.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ENVRC = Path(__file__).resolve().parents[1] / ".envrc"


@pytest.mark.parametrize(
    "line",
    [
        r"export\s+DISABLE_TELEMETRY=",
        r"export\s+CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=",
        r"export\s+DISABLE_GROWTHBOOK=",
        r"unset\s+CLAUDE_CODE_ENABLE_TELEMETRY\b",
    ],
)
def test_no_channel_blocking_line_is_active(line: str) -> None:
    active = [
        text
        for text in _ENVRC.read_text().splitlines()
        if re.match(rf"\s*{line}", text)
    ]
    assert active == [], f".envrc re-enables a channel blocker: {active}"


def test_agent_teams_stays_on() -> None:
    assert re.search(
        r"^export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1$",
        _ENVRC.read_text(),
        re.MULTILINE,
    )
