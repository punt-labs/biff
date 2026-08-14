"""Regression tests: displayed idle vs heartbeat.

The IDLE column in ``/who`` and the ``idle H:MM`` field in ``/finger`` used
to be computed from ``UserSession.last_active`` — the same field the
background heartbeat rewrites to ``now()`` on every tick regardless of
whether anything happened.  Every live session's displayed idle time could
therefore never read as more than 0-1 minutes, no matter how long it had
actually sat unused.

``last_tool_at`` is the fix: a second timestamp written only by
``update_current_session`` (reached from every ``track_activity``-decorated
tool body) on a real tool invocation.  These tests exercise the fix at the
display layer — the same ``format_who``/``format_finger`` functions the
tools call — so they demonstrate the actual bug, not just a model field.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from biff._formatting import HEADER_PREFIX, ROW_PREFIX
from biff.formatting import format_finger, format_who
from biff.models import BiffConfig
from biff.server.state import create_state
from biff.server.tools._session import update_current_session

_TEST_REPO = "_test-activity-timestamp"


def _finger_idle_value(rendered: str) -> str:
    """Return the exact ``idle`` field value from a ``format_finger`` block.

    A plain substring check (``"idle 0:10" in rendered``) would still pass
    if the format grew a seconds component (``idle 0:10:47``) or rendered a
    malformed value (``idle 0:100``) that merely starts with the expected
    text -- this isolates the whole field so the comparison is exact.
    """
    on_since_line = next(
        line for line in rendered.splitlines() if line.strip().startswith("On since")
    )
    return on_since_line.rsplit("idle ", 1)[1]


def _who_idle_cell(rendered: str) -> str:
    """Return the IDLE column value from a single-row /who table.

    WHO_SPECS column order is NAME, K, REPO, IDLE, S, P, HOST. The caller
    must give every session a non-empty ``kind`` so the K column never
    renders blank -- otherwise a plain whitespace split would collapse it
    and misalign the remaining columns.
    """
    header, row = rendered.splitlines()[:2]
    header_cells = header.removeprefix(HEADER_PREFIX).split()
    row_cells = row.removeprefix(ROW_PREFIX).split()
    idle_index = header_cells.index("IDLE")
    return row_cells[idle_index]


class TestIdleDisplaySurvivesHeartbeat:
    """Heartbeat ticks alone must not advance the displayed idle time."""

    async def test_finger_idle_reflects_real_inactivity_not_heartbeat(
        self, tmp_path: Path
    ) -> None:
        config = BiffConfig(user="kai", repo_name=_TEST_REPO)
        state = create_state(config, tmp_path, tty="tty1")
        await update_current_session(state)  # real activity baseline

        session = await state.relay.get_session(state.session_key)
        assert session is not None
        backdated = datetime.now(UTC) - timedelta(minutes=10)
        await state.relay.update_session(
            session.model_copy(
                update={"last_active": backdated, "last_tool_at": backdated}
            )
        )

        # The background heartbeat loop's only job: several ticks, no real
        # tool calls in between.
        for _ in range(3):
            await state.relay.heartbeat(state.session_key)

        session = await state.relay.get_session(state.session_key)
        assert session is not None
        rendered = format_finger(session)
        assert _finger_idle_value(rendered) == "0:10", (
            f"heartbeat ticks advanced the displayed idle time: {rendered!r}"
        )

    async def test_who_idle_reflects_real_inactivity_not_heartbeat(
        self, tmp_path: Path
    ) -> None:
        # kind="agent" gives the K column non-empty content ("[A]"), so
        # every WHO_SPECS column below renders a non-blank cell and the
        # row can be parsed by plain whitespace-splitting (_who_idle_cell).
        config = BiffConfig(user="kai", repo_name=_TEST_REPO, kind="agent")
        state = create_state(config, tmp_path, tty="tty1")
        await update_current_session(state)

        session = await state.relay.get_session(state.session_key)
        assert session is not None
        backdated = datetime.now(UTC) - timedelta(minutes=10)
        await state.relay.update_session(
            session.model_copy(
                update={"last_active": backdated, "last_tool_at": backdated}
            )
        )

        for _ in range(3):
            await state.relay.heartbeat(state.session_key)

        sessions = await state.relay.get_sessions()
        rendered = format_who(sessions)
        idle_cell = _who_idle_cell(rendered)
        assert idle_cell == "10m", (
            f"heartbeat ticks advanced the displayed idle time: {rendered!r}"
        )


class TestIdleResetsOnRealActivity:
    """A real tool invocation must reset the displayed idle time."""

    async def test_finger_idle_resets_on_real_tool_invocation(
        self, tmp_path: Path
    ) -> None:
        config = BiffConfig(user="kai", repo_name=_TEST_REPO)
        state = create_state(config, tmp_path, tty="tty1")
        await update_current_session(state)

        session = await state.relay.get_session(state.session_key)
        assert session is not None
        backdated = datetime.now(UTC) - timedelta(minutes=10)
        await state.relay.update_session(
            session.model_copy(
                update={"last_active": backdated, "last_tool_at": backdated}
            )
        )

        await update_current_session(state)  # a real tool invocation

        session = await state.relay.get_session(state.session_key)
        assert session is not None
        rendered = format_finger(session)
        assert _finger_idle_value(rendered) == "0:00"


class TestIdleIsolatedPerUser:
    """A presence query by a different user must not reset this user's idle."""

    async def test_other_users_who_call_does_not_reset_idle(
        self, tmp_path: Path
    ) -> None:
        config_a = BiffConfig(user="kai", repo_name=_TEST_REPO)
        state_a = create_state(config_a, tmp_path, tty="tty1")
        await update_current_session(state_a)

        session_a = await state_a.relay.get_session(state_a.session_key)
        assert session_a is not None
        backdated = datetime.now(UTC) - timedelta(minutes=10)
        await state_a.relay.update_session(
            session_a.model_copy(
                update={"last_active": backdated, "last_tool_at": backdated}
            )
        )

        # A different user, sharing the same data dir, makes a real tool
        # call of their own (e.g. /who).
        config_b = BiffConfig(user="eric", repo_name=_TEST_REPO)
        state_b = create_state(config_b, tmp_path, tty="tty2")
        await update_current_session(state_b)

        after_a = await state_a.relay.get_session(state_a.session_key)
        assert after_a is not None
        assert after_a.last_tool_at == backdated


class TestRegistrationSetsLastToolAt:
    """A freshly registered session idles from its own start time."""

    async def test_register_session_sets_last_tool_at(self, tmp_path: Path) -> None:
        from biff.server.app import register_session

        session, _ = await register_session(
            create_state(
                BiffConfig(user="kai", repo_name=_TEST_REPO), tmp_path, tty="tty1"
            ).relay,
            "kai",
            "a1b2c3d4",
            display_name="Kai",
            kind="human",
            hostname="test-host",
            pwd="/test",
            repo=_TEST_REPO,
        )
        assert session.last_tool_at == session.last_active
