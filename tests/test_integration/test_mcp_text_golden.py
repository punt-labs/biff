"""Every MCP tool returns, byte for byte, the text the display pipeline expects.

The panel line and the model's context are cut from a tool's text by
``plugin/hooks/suppress-output.sh``, so one changed character can break a
column count or a prefix match.  This is the oracle that any other way of
serving the tools (a thin client of biffd) must reproduce.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from fastmcp.client.transports import FastMCPTransport

from biff.models import BiffConfig, SessionEvent
from biff.nostr_directory import NostrDirectory
from biff.server.app import create_server
from biff.server.tools import register_channel_tools
from biff.server.tools.channel_ack import NOT_ACCEPTED
from biff.testing import RecordingClient, Transcript
from biff.testing.caller import StampedClient, bound_to
from tests._mcp_golden import STEPS, HistoryRelay, Step, isolate_machine, normalise
from tests._state import create_state

if TYPE_CHECKING:
    from pathlib import Path


def _differences(steps: tuple[Step, ...], actual: list[str]) -> list[str]:
    """Name each step whose normalised text is not its golden text."""
    return [
        f"{step.user} {step.tool} {step.args}:\n  want {step.text!r}\n  got  {got!r}"
        for step, text in zip(steps, actual, strict=True)
        if (got := normalise(text, step.volatile)) != step.text
    ]


@pytest.fixture(autouse=True)
def _private_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolate_machine(monkeypatch, tmp_path / "machine")


async def test_every_tool_returns_its_golden_text(
    kai: RecordingClient, eric: RecordingClient
) -> None:
    clients = {"kai": kai, "eric": eric}
    actual = [await clients[s.user].call(s.tool, **s.args) for s in STEPS]

    assert _differences(STEPS, actual) == []


@pytest.mark.parametrize("index", range(len(STEPS)))
def test_a_one_character_change_in_any_golden_text_is_caught(index: int) -> None:
    step = STEPS[index]
    changed = step.text[:-1] + ("." if step.text[-1] != "." else "!")

    assert _differences((step,), [changed]) != []


class TestTheClockIsNormalisedWithinABand:
    """Time-dependent text is normalised only where the clock can put it."""

    @pytest.mark.parametrize("minutes", [58, 59, 60])
    def test_the_minutes_an_hour_wall_can_have_left_read_the_same(
        self, minutes: int
    ) -> None:
        text = f"Wall posted ({minutes}m):\n   freeze now"

        assert normalise(text, ("minutes_left",)) == (
            "Wall posted (<58-60>m):\n   freeze now"
        )

    @pytest.mark.parametrize("minutes", [0, 10, 30, 57, 61])
    def test_any_other_minutes_are_left_for_the_comparison_to_refuse(
        self, minutes: int
    ) -> None:
        text = f"Wall posted ({minutes}m):\n   freeze now"

        assert normalise(text, ("minutes_left",)) == text

    @pytest.mark.parametrize("idle", ["0:00", "0:01"])
    def test_an_idle_time_of_a_moment_ago_reads_the_same(self, idle: str) -> None:
        assert normalise(f"idle {idle}", ("idle",)) == "idle <0:00-0:01>"

    @pytest.mark.parametrize("idle", ["0:09", "1:00", "5:30"])
    def test_a_longer_idle_time_is_left_for_the_comparison_to_refuse(
        self, idle: str
    ) -> None:
        assert normalise(f"idle {idle}", ("idle",)) == f"idle {idle}"


class TestThePlanMarkerAndTheKeysStayOffThisMachine:
    async def test_a_plan_writes_its_marker_under_the_private_home(
        self, kai: RecordingClient, tmp_path: Path
    ) -> None:
        await kai.call("plan", message="golden")

        hints = tmp_path / "machine" / "home" / ".punt-labs" / "biff" / "hints"
        assert any(path.is_file() for path in hints.rglob("*"))

    def test_the_keys_of_a_who_table_come_from_an_empty_file_of_its_own(
        self, tmp_path: Path
    ) -> None:
        directory = NostrDirectory.for_repo(None)

        directory.publish("kai", {"npub": "npub1golden"})

        assert (tmp_path / "machine" / "nostr.yaml").is_file()


def _event(
    session_key: str, event: str, user: str, tty_name: str, when: datetime
) -> SessionEvent:
    return SessionEvent(
        session_key=session_key,
        event=event,
        user=user,
        tty=session_key.partition(":")[2],
        tty_name=tty_name,
        hostname="test-host",
        repo="_test-integration",
        timestamp=when,
    )


async def test_last_prints_a_table_when_there_is_history(tmp_path: Path) -> None:
    """One finished session, one that is still logged in, and one that is gone."""
    config = BiffConfig(user="kai", repo_name="_test-integration")
    relay = HistoryRelay(tmp_path)
    state = bound_to(create_state(config, tmp_path, relay=relay, hostname="test-host"))
    at = datetime(2026, 11, 20, tzinfo=UTC)
    for event in (
        _event("eric:e1", "login", "eric", "tty1", at.replace(hour=9)),
        _event("eric:e1", "logout", "eric", "tty1", at.replace(hour=10, minute=30)),
        _event("ghost:g1", "login", "ghost", "tty2", at.replace(hour=8)),
        _event(state.session_key, "login", "kai", "kaitty", at.replace(hour=11)),
    ):
        await relay.append_wtmp(event)
    async with StampedClient(FastMCPTransport(create_server(state))) as client:
        kai = RecordingClient(client=client, transcript=Transcript(title=""))

        text = await kai.call("last")

    # The server's own login is stamped now; the seeded events have fixed dates.
    stamped_now = re.compile(
        r"(?<=test-host  )(?!Fri Nov 20 )"
        r"[A-Z][a-z]{2} [A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}"
    )
    assert stamped_now.sub("<NOW>", text) == "\n".join(
        [
            (
                "▶  NAME        REPO               HOST       LOGIN             "
                "LOGOUT            DURATION"
            ),
            (
                "   kai:tty1    _test-integration  test-host  <NOW>  "
                "still logged in   -       "
            ),
            (
                "   kai:kaitty  _test-integration  test-host  Fri Nov 20 11:00  "
                "still logged in   -       "
            ),
            (
                "   eric:tty1   _test-integration  test-host  Fri Nov 20 09:00  "
                "Fri Nov 20 10:30  1:30    "
            ),
            (
                "   ghost:tty2  _test-integration  test-host  Fri Nov 20 08:00  "
                "Fri Nov 20 08:00  0:00    "
            ),
        ]
    )


async def test_channel_ack_refuses_a_token_it_never_issued(tmp_path: Path) -> None:
    """Called as the model calls it: through the MCP client, by its tool name."""
    state = create_state(
        BiffConfig(user="kai", repo_name="_test-golden"),
        tmp_path,
        tty="tty1",
        hostname="h",
        pwd="/p",
        repo_root=tmp_path,
        unread_path=tmp_path / "unread.json",
    )
    mcp = create_server(state)
    register_channel_tools(mcp, state)
    async with StampedClient(FastMCPTransport(mcp)) as client:
        kai = RecordingClient(client=client, transcript=Transcript(title=""))

        text = await kai.call("channel_ack", token="nope")

    assert (text, NOT_ACCEPTED) == (
        "biff channel token not accepted; nothing changed.",
        "biff channel token not accepted; nothing changed.",
    )
