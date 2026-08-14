"""Regression test for biff-liu round 2: /who row order matches the IDLE column.

The IDLE column of ``/who`` renders ``last_tool_at`` (biff-liu), but the
row order was still computed from ``last_active`` -- heartbeat recency,
not real activity. Once every visible session heartbeats within the same
liveness window, ``last_active`` order is effectively arbitrary; the
visible order must instead follow the same field the IDLE column shows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from biff.models import UserSession
from biff.server.state import ServerState
from biff.testing import RecordingClient


class TestWhoOrdersByLastToolAt:
    async def test_row_order_follows_last_tool_at_not_last_active(
        self, recorder: RecordingClient, state: ServerState
    ) -> None:
        # Two OTHER users, not "kai" -- the recorder's own call registers
        # (or refreshes) a "kai" session as a side effect, which would
        # collide with a "kai" fixture session and confuse the ordering
        # assertion below.
        now = datetime.now(UTC)
        # alice heartbeated most recently (highest last_active) but has
        # been idle the longest (oldest last_tool_at); bob is the reverse.
        await state.relay.update_session(
            UserSession(
                user="alice",
                tty="aaa11111",
                tty_name="tty1",
                last_active=now,
                last_tool_at=now - timedelta(minutes=10),
            )
        )
        await state.relay.update_session(
            UserSession(
                user="bob",
                tty="bbb22222",
                tty_name="tty2",
                last_active=now - timedelta(seconds=5),
                last_tool_at=now,
            )
        )

        result = await recorder.call("who")

        assert result.index("bob") < result.index("alice"), (
            "row order must follow last_tool_at (matching the IDLE column), "
            f"not heartbeat recency:\n{result}"
        )
