"""Every MCP tool returns, byte for byte, the text the display pipeline expects.

The panel line and the model's context are cut from a tool's text by
``plugin/hooks/suppress-output.sh``, so one changed character can break a
column count or a prefix match.  This is the oracle that any other way of
serving the tools (a thin client of biffd) must reproduce.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from fastmcp.client.transports import FastMCPTransport

from biff.models import BiffConfig
from biff.nostr_directory import NostrDirectory
from biff.server.app import create_server
from biff.server.tools import register_channel_tools
from biff.server.tools.channel_ack import NOT_ACCEPTED
from biff.testing import RecordingClient, Transcript
from biff.testing.caller import StampedClient, bound_to
from tests._mcp_golden import (
    STEPS,
    HistoryRelay,
    differences,
    isolate_machine,
    last_table,
    normalise,
)
from tests._state import create_state

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _private_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolate_machine(monkeypatch, tmp_path / "machine")


async def test_every_tool_returns_its_golden_text(
    kai: RecordingClient, eric: RecordingClient
) -> None:
    clients = {"kai": kai, "eric": eric}
    actual = [await clients[s.user].call(s.tool, **s.args) for s in STEPS]

    assert differences(STEPS, actual) == []


@pytest.mark.parametrize("index", range(len(STEPS)))
def test_a_one_character_change_in_any_golden_text_is_caught(index: int) -> None:
    step = STEPS[index]
    changed = step.text[:-1] + ("." if step.text[-1] != "." else "!")

    assert differences((step,), [changed]) != []


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


@pytest.mark.parametrize(
    ("now", "login", "rank"),
    [
        pytest.param(
            datetime(2026, 12, 1, 9, 15, tzinfo=UTC), "Tue Dec 01 09:15", 0, id="after"
        ),
        pytest.param(
            datetime(2026, 11, 20, 8, 30, tzinfo=UTC),
            "Fri Nov 20 08:30",
            1,
            id="same-day",
        ),
        pytest.param(
            datetime(2026, 1, 5, 7, 0, tzinfo=UTC), "Mon Jan 05 07:00", 2, id="before"
        ),
    ],
)
async def test_last_prints_the_same_table_whatever_the_date(
    tmp_path: Path, now: datetime, login: str, rank: int
) -> None:
    """A finished session, a live one, and one that is gone, under three clocks.

    The server's own login takes the injected time, so the table cannot depend
    on the calendar; where it falls among the seeded rows is the one thing the
    date changes, and ``rank`` says where.  The ghost's login arrives after the
    server attached: at attach, ``_close_orphaned_logins`` would close it.
    """
    config = BiffConfig(user="kai", repo_name="_test-integration")
    probe = create_state(config, tmp_path / "probe", hostname="test-host")
    key = bound_to(probe).session_key
    relay = HistoryRelay(tmp_path, own_key=key, clock=now)
    state = bound_to(create_state(config, tmp_path, relay=relay, hostname="test-host"))
    assert state.session_key == key
    async with StampedClient(FastMCPTransport(create_server(state))) as client:
        kai = RecordingClient(client=client, transcript=Transcript(title=""))
        await kai.call("who")  # attaches the session
        await relay.seed_other_sessions()

        text = await kai.call("last")

    assert text == last_table(login, rank)


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
