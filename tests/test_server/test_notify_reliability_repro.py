"""Reproduction of the dropped-notification-with-no-recovery defect.

``notify_tool_list_changed``'s suspenders path (``_descriptions.py``) sends
directly on the stored ``_session`` when no request context is active. When
that send raises — a dead or reconnecting stream — the current code clears
``_session`` and returns *without* recording ``_pending_notify``. The tool
description was already mutated by the caller before the notify call, so
every later ``refresh_*`` sees no description change and never calls
``notify_tool_list_changed`` again. The dropped notification is never
retried, and a subsequent session recapture (reconnect) has nothing to
flush, because ``capture_session`` only flushes when ``_pending_notify`` is
set. Net effect: the client's cached tool list silently goes stale forever.

These tests assert the desired behavior — a dropped mid-session
notification is retried or flushed on the next opportunity — and are
expected to fail against the current implementation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.session import ServerSession

from biff.models import BiffConfig, Message
from biff.server.app import create_server
from biff.server.state import ServerState, create_state
from biff.server.tools import _descriptions
from biff.server.tools._descriptions import refresh_read_messages

_TEST_REPO = "_test-server"
_KAI_SESSION = "kai:tty1"


@pytest.fixture
def state(tmp_path: Path) -> ServerState:
    return create_state(
        BiffConfig(user="kai", repo_name=_TEST_REPO),
        tmp_path,
        tty="tty1",
        hostname="test-host",
        pwd="/test",
    )


class TestDroppedMidSessionNotificationHasNoRecovery:
    """A send failure on an already-captured session must not lose the
    notification permanently — it must be retried or flushed later.
    """

    async def test_mid_session_send_failure_records_pending_notify(
        self, state: ServerState
    ) -> None:
        """When the suspenders-path send raises, the drop must be recorded
        as pending so a later flush opportunity (e.g. session recapture)
        can retry it.

        Currently the suspenders branch clears ``_session`` and returns
        on failure without ever touching ``_pending_notify`` — that flag
        is only set in the pre-session branch. This assertion is the
        contract the code should uphold and does not.
        """
        mcp = create_server(state)
        await state.relay.deliver(
            Message(from_user="eric", to_user=_KAI_SESSION, body="hello")
        )

        dying_session = MagicMock(spec=ServerSession)
        dying_session.send_tool_list_changed = AsyncMock(
            side_effect=RuntimeError("transport closed")
        )
        _descriptions._session = dying_session

        await refresh_read_messages(mcp, state)  # mutates description, then
        # calls notify_tool_list_changed(), which hits the suspenders path
        # and fails to send.

        dying_session.send_tool_list_changed.assert_awaited_once()
        # Assert _pending_notify BEFORE _session is None: mypy's attribute
        # narrowing treats the direct `_descriptions._session = dying_session`
        # assignment above as persisting past the awaited call, so an `is
        # None` check ordered first makes mypy consider the next statement
        # unreachable. The reverse order type-checks cleanly.
        assert _descriptions._pending_notify  # FAILS: never set on this path
        assert _descriptions._session is None  # dead session correctly cleared

    async def test_reconnect_after_dropped_notification_flushes_once(
        self, state: ServerState
    ) -> None:
        """After a mid-session send failure, the next session capture
        (a reconnect) must flush the notification the client never saw.

        Reproduces the full recovery gap: the description already changed
        server-side, the dropped push is never retried, and the next
        ``capture_session`` — which only flushes when ``_pending_notify``
        is set — has nothing to flush because that flag was never set.
        """
        mcp = create_server(state)
        await state.relay.deliver(
            Message(from_user="eric", to_user=_KAI_SESSION, body="hi")
        )

        dying_session = MagicMock(spec=ServerSession)
        dying_session.send_tool_list_changed = AsyncMock(
            side_effect=RuntimeError("transport closed")
        )
        _descriptions._session = dying_session

        await refresh_read_messages(mcp, state)  # drops the notification

        reconnected_session = MagicMock(spec=ServerSession)
        reconnected_session.send_tool_list_changed = AsyncMock()
        await _descriptions.capture_session(reconnected_session)

        # FAILS: capture_session's early return (`if not _pending_notify:
        # return`) skips the flush, so the reconnected client never learns
        # its tool list is stale — even though the description already
        # diverged from base and the client was never told.
        reconnected_session.send_tool_list_changed.assert_awaited_once()
