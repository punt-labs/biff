"""Talk publish failures never strand a session: rollback, restore, retry.

The kernel in ``biff.commands.talk`` is what the daemon, the MCP tools and the
REPL's biffd methods all run, so its failure handling is tested directly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import biff.commands.talk as talk_commands
from biff.models import UserSession
from biff.nats_relay import NatsRelay
from biff.talk_state import TalkState
from biff.talk_types import TalkPhase

if TYPE_CHECKING:
    import pytest

    from biff.relay import Relay


class TestWithdrawTalkInviteResilience:
    """The best-effort withdraw failure stays off the WARNING stderr floor.

    The CLI raises the stderr handler to WARNING, so a WARNING here would dump
    a traceback into the interactive REPL; INFO keeps it in biff.log while the
    local state still resets and the invitee falls back to the TTL sweep.
    """

    async def test_publish_failure_logs_at_info_and_resets(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        relay = MagicMock(spec=NatsRelay)
        relay.get_nc = AsyncMock(side_effect=TimeoutError("private-login"))
        relay.get_session = AsyncMock(return_value=None)
        talk = TalkState(
            relay=cast("Relay", relay),
            user="kai",
            tty="abc",
            session_key="kai:abc",
        )
        talk.begin_invite(partner="eric", partner_tty="def", partner_key="eric:def")
        ctx = MagicMock()
        ctx.talk = talk
        ctx.relay = relay
        ctx.session_key = "kai:abc"

        # The withdraw delegates to the shared end_or_cancel kernel, so the
        # best-effort publish failure is logged under biff.commands.talk.
        with caplog.at_level(logging.INFO, logger="biff.commands.talk"):
            await talk_commands.end_or_cancel(ctx)

        assert talk.phase is TalkPhase.IDLE  # local state reset despite the failure
        records = [r for r in caplog.records if r.name == "biff.commands.talk"]
        assert records  # the failure was logged
        assert all(r.levelno == logging.INFO for r in records)
        assert "TimeoutError" in records[0].getMessage()
        assert "private-login" not in records[0].getMessage()
        assert records[0].exc_info is None


class TestInviteRollback:
    """A transient invite publish must not strand the REPL session.

    The ``invite`` kernel resets the phase on a failed ``send_invite``, so a
    failed invite leaves the phase idle and never writes a phantom
    ``talking to …`` presence.
    """

    async def test_invite_publish_failure_leaves_phase_idle_no_plan(self) -> None:
        relay = MagicMock(spec=NatsRelay)
        relay.get_nc = AsyncMock(side_effect=TimeoutError("wedged"))
        relay.get_sessions_for_repos = AsyncMock(
            return_value=[
                UserSession(user="eric", tty="def456", tty_name="tty2", repo="myrepo")
            ]
        )
        relay.get_session = AsyncMock(
            return_value=UserSession(
                user="kai", tty="kaihex01", tty_name="tty1", plan="idle", repo="myrepo"
            )
        )
        relay.update_session = AsyncMock()
        talk = TalkState(
            relay=cast("Relay", relay),
            user="kai",
            tty="kaihex01",
            session_key="kai:kaihex01",
            tty_name="tty1",
        )
        ctx = MagicMock()
        ctx.talk = talk
        ctx.relay = relay
        ctx.session_key = "kai:kaihex01"
        ctx.user = "kai"
        ctx.tty_name = "tty1"
        ctx.config.repo_name = "myrepo"
        ctx.visible_repos = frozenset({"myrepo"})

        result = await talk_commands.talk(ctx, "@eric:tty2", "")

        assert result.error is True
        assert talk.phase is TalkPhase.IDLE  # kernel rolled the invite back
        # The plan is written only after a successful publish — a failed invite
        # must never leave a phantom ``talking to …`` presence.
        planned = [c.args[0].plan for c in relay.update_session.await_args_list]
        assert all("talking to" not in p for p in planned)


class TestAcceptRestoresPendingInvite:
    """A responder whose accept publish fails keeps the invite acceptable.

    Consuming the invite
    before the accept and not restoring it would leave a retry sending a fresh
    outbound invite instead of re-accepting.
    """

    @staticmethod
    def _invite_frame(to_key: str) -> dict[str, str]:
        return {
            "type": "invite",
            "from": "jfreeman",
            "from_tty": "tty6",
            "from_key": "jfreeman:75abc665",
            "body": "wants to talk",
            "to_key": to_key,
        }

    async def test_accept_publish_failure_restores_invite(self) -> None:
        relay = MagicMock(spec=NatsRelay)
        relay.get_nc = AsyncMock(side_effect=TimeoutError("relay wedged"))
        relay.talk_notify_subject = MagicMock(return_value="biff.t.talk.notify.jf")
        relay.get_sessions_for_repos = AsyncMock(
            return_value=[
                UserSession(
                    user="jfreeman", tty="75abc665", tty_name="tty6", repo="myrepo"
                )
            ]
        )
        relay.get_session = AsyncMock(return_value=None)
        relay.update_session = AsyncMock()
        talk = TalkState(
            relay=cast("Relay", relay),
            user="kai",
            tty="kaihex01",
            session_key="kai:kaihex01",
            tty_name="tty1",
        )
        talk.receive(self._invite_frame("kai:kaihex01"))
        talk.drain_idle()  # record the pending invite
        assert "jfreeman" in talk.pending_invites
        ctx = MagicMock()
        ctx.talk = talk
        ctx.relay = relay
        ctx.session_key = "kai:kaihex01"
        ctx.user = "kai"
        ctx.tty_name = "tty1"
        ctx.config.repo_name = "myrepo"
        ctx.visible_repos = frozenset({"myrepo"})

        await talk_commands.talk(ctx, "@jfreeman", "")

        assert talk.phase is TalkPhase.IDLE  # not stranded CONNECTED
        assert "jfreeman" in talk.pending_invites  # restored for a retry


class TestPublishAutoAccept:
    """The higher-key auto-accept must actually reach the partner (F2).

    The lower-key side connects ONLY on receiving this accept (talk.tex
    MutualAutoAccept — no symmetric fallback), so a dropped accept strands it.
    The publish retries once, and the handshake warns the user when both fail.
    """

    @staticmethod
    def _talk(*, get_nc: AsyncMock) -> TalkState:
        relay = MagicMock(spec=NatsRelay)
        relay.get_nc = get_nc
        relay.talk_notify_subject = MagicMock(return_value="biff.t.talk.notify.eric")
        return TalkState(
            relay=cast("Relay", relay),
            user="kai",
            tty="kaihex01",
            session_key="kai:kaihex01",
            tty_name="tty1",
        )

    async def test_retries_once_then_gives_up_on_persistent_failure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        get_nc = AsyncMock(side_effect=TimeoutError("private-login"))
        talk = self._talk(get_nc=get_nc)
        ctx = MagicMock()
        ctx.talk = talk

        with caplog.at_level(logging.INFO, logger="biff.commands.talk"):
            ok = await talk_commands.publish_auto_accept(ctx, to_key="eric:def456")

        assert ok is False
        assert get_nc.await_count == 2  # published once, retried once
        records = [r for r in caplog.records if r.name == "biff.commands.talk"]
        assert len(records) == 2
        assert all("TimeoutError" in r.getMessage() for r in records)
        assert all(r.exc_info is None for r in records)
        assert "private-login" not in caplog.text
        assert "eric" not in caplog.text

    async def test_succeeds_without_retry_when_publish_works(self) -> None:
        get_nc = AsyncMock(return_value=AsyncMock())
        talk = self._talk(get_nc=get_nc)
        ctx = MagicMock()
        ctx.talk = talk

        ok = await talk_commands.publish_auto_accept(ctx, to_key="eric:def456")

        assert ok is True
        assert get_nc.await_count == 1  # no retry on success
