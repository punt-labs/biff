"""Regression tests for biff-hvi: CLI session fallback repo/ttyN drift.

Before this, the CLI fallback in ``plan``/``mesg``/``tty`` independently
rebuilt a missing session record from the calling process's environment
and omitted ``repo`` (defaulting to ``""``), while ``plan``/``mesg``
additionally hardcoded ``tty_name="cli"``, discarding the claimed ttyN.
``/who`` renders ``session.repo``; ``/finger`` renders ``session.pwd`` --
two independent fields on the same record -- and a later reader
backfilling ``repo`` from a DIFFERENT process's config left ``repo`` and
``pwd`` disagreeing about which repo the session belonged to.
``biff.commands._session.update_current_session`` is now the single
construction path, so both fields come from the process actually
writing the record.
"""

from __future__ import annotations

from biff.cli_session import CliContext
from biff.commands._session import update_current_session
from biff.commands.mesg import mesg
from biff.commands.plan import plan
from biff.models import BiffConfig
from biff.relay import LocalRelay
from biff.tty import get_pwd


class TestFallbackThreadsRepoAndTtyName:
    async def test_missing_session_carries_owning_repo(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        session = await update_current_session(ctx)
        assert session.repo == ctx.config.repo_name

    async def test_missing_session_preserves_claimed_tty_name(
        self, relay: LocalRelay
    ) -> None:
        ctx = CliContext(
            relay=relay,
            config=BiffConfig(user="kai", repo_name="lux"),
            session_key="kai:claimed1",
            user="kai",
            tty="claimed1",
            tty_name="tty7",
        )
        session = await update_current_session(ctx)
        assert session.tty_name == "tty7"

    async def test_plan_fallback_threads_repo_and_claimed_tty_name(
        self, relay: LocalRelay
    ) -> None:
        ctx = CliContext(
            relay=relay,
            config=BiffConfig(user="kai", repo_name="vox"),
            session_key="kai:aaa11111",
            user="kai",
            tty="aaa11111",
            tty_name="tty3",
        )
        await plan(ctx, "hi")
        session = await relay.get_session("kai:aaa11111")
        assert session is not None
        assert session.repo == "vox"
        assert session.tty_name == "tty3"

    async def test_mesg_fallback_threads_repo_and_claimed_tty_name(
        self, relay: LocalRelay
    ) -> None:
        ctx = CliContext(
            relay=relay,
            config=BiffConfig(user="kai", repo_name="lux"),
            session_key="kai:bbb22222",
            user="kai",
            tty="bbb22222",
            tty_name="tty4",
        )
        await mesg(ctx, "on")
        session = await relay.get_session("kai:bbb22222")
        assert session is not None
        assert session.repo == "lux"
        assert session.tty_name == "tty4"


class TestRepoAndPwdNeverDisagree:
    async def test_fallback_sources_repo_and_pwd_from_the_same_process(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        """A fallback-created record's repo (matching the owning server's
        config) and pwd (this process's cwd) are written together, in one
        place -- so a later reader that backfills only a missing repo
        (server/tools/_session.py's get_or_create_session) never finds an
        empty repo to fill in from a DIFFERENT process than the one that
        set pwd."""
        session = await update_current_session(ctx)
        assert session.repo == ctx.config.repo_name
        assert session.pwd == get_pwd()
