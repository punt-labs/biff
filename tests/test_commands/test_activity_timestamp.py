"""Regression tests: CLI commands must advance idle.

The MCP surface has ``update_current_session`` writing ``last_tool_at``
on every real tool invocation. The CLI ``plan``,
``tty``, and ``mesg`` commands each wrote their session update inline via
``model_copy`` and never touched ``last_tool_at`` -- for a long-lived
interactive REPL session (``cli_session(interactive=True)``) that meant
``last_tool_at`` stayed pinned at registration forever, so an actively-used
REPL showed a monotonically growing idle time instead of resetting on
every command. ``biff.commands._session.update_current_session`` is the
shared fix; these tests exercise it through each CLI command.

The REPL's talk sub-loop had the same bug: ``_set_talk_plan`` and
``_clear_talk_plan`` (``biff.__main__``) wrote the ``plan`` field via a
bare ``model_copy`` and never routed through ``update_current_session``,
so a long-lived ``talk`` conversation read as increasingly idle even
while the operator was actively talking.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from biff.__main__ import _clear_talk_plan, _set_talk_plan
from biff.cli_session import CliContext
from biff.commands.mesg import mesg
from biff.commands.plan import plan
from biff.commands.tty import tty
from biff.relay import LocalRelay


async def _backdate(relay: LocalRelay, session_key: str, when: datetime) -> None:
    session = await relay.get_session(session_key)
    assert session is not None
    await relay.update_session(
        session.model_copy(update={"last_active": when, "last_tool_at": when})
    )


class TestCliActivityResetsOnRealCommand:
    """A real CLI command must reset last_tool_at, not just last_active."""

    async def test_plan_resets_idle(self, ctx: CliContext, relay: LocalRelay) -> None:
        await plan(ctx, "baseline")
        backdated = datetime.now(UTC) - timedelta(minutes=10)
        await _backdate(relay, ctx.session_key, backdated)

        await plan(ctx, "still working")

        session = await relay.get_session(ctx.session_key)
        assert session is not None
        assert session.last_tool_at > backdated
        assert session.last_active > backdated

    async def test_mesg_resets_idle(self, ctx: CliContext, relay: LocalRelay) -> None:
        await mesg(ctx, "on")
        backdated = datetime.now(UTC) - timedelta(minutes=10)
        await _backdate(relay, ctx.session_key, backdated)

        await mesg(ctx, "off")

        session = await relay.get_session(ctx.session_key)
        assert session is not None
        assert session.last_tool_at > backdated
        assert session.last_active > backdated

    async def test_tty_resets_idle(self, ctx: CliContext, relay: LocalRelay) -> None:
        await tty(ctx, "dev")
        backdated = datetime.now(UTC) - timedelta(minutes=10)
        await _backdate(relay, ctx.session_key, backdated)

        await tty(ctx, "dev2")

        session = await relay.get_session(ctx.session_key)
        assert session is not None
        assert session.last_tool_at > backdated
        assert session.last_active > backdated


class TestReplTalkActivityResetsOnRealActivity:
    """The REPL talk sub-loop must reset last_tool_at, not just last_active."""

    async def test_set_talk_plan_resets_idle(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        await plan(ctx, "baseline")
        backdated = datetime.now(UTC) - timedelta(minutes=10)
        await _backdate(relay, ctx.session_key, backdated)

        await _set_talk_plan(ctx, "eric:tty2")

        session = await relay.get_session(ctx.session_key)
        assert session is not None
        assert session.plan == "talking to eric:tty2"
        assert session.last_tool_at > backdated
        assert session.last_active > backdated

    async def test_clear_talk_plan_resets_idle(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        await _set_talk_plan(ctx, "eric:tty2")
        backdated = datetime.now(UTC) - timedelta(minutes=10)
        await _backdate(relay, ctx.session_key, backdated)

        await _clear_talk_plan(ctx)

        session = await relay.get_session(ctx.session_key)
        assert session is not None
        assert session.plan == ""
        assert session.last_tool_at > backdated
        assert session.last_active > backdated
