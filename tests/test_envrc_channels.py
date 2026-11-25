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


# Bash exports the same value with or without quotes around NAME=VALUE.
_BLOCKERS = re.compile(
    r"""^\s*(?:export\s+["']?(?:DISABLE_TELEMETRY|DISABLE_GROWTHBOOK"""
    r"""|CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC)="""
    r"""|unset\s+["']?CLAUDE_CODE_ENABLE_TELEMETRY\b)"""
)


def _active(lines: list[str]) -> list[str]:
    """Return the lines that would set a channel blocker when sourced."""
    return [text for text in lines if _BLOCKERS.match(text)]


def test_no_channel_blocking_line_is_active() -> None:
    active = _active(_ENVRC.read_text().splitlines())
    assert active == [], f".envrc re-enables a channel blocker: {active}"


@pytest.mark.parametrize(
    "text",
    [
        "export DISABLE_TELEMETRY=1",
        'export "DISABLE_TELEMETRY=1"',
        "export 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1'",
        "  export DISABLE_GROWTHBOOK=1",
        'unset "CLAUDE_CODE_ENABLE_TELEMETRY"',
    ],
)
def test_the_guard_catches_every_spelling(text: str) -> None:
    assert _active([text])


def test_the_guard_ignores_comments() -> None:
    assert not _active(["# export DISABLE_TELEMETRY=1"])


def test_agent_teams_stays_on() -> None:
    assert re.search(
        r"^export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1$",
        _ENVRC.read_text(),
        re.MULTILINE,
    )
