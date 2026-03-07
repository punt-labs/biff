"""Tests for the command dispatcher (biff.dispatch).

Tests command parsing and dispatch against a LocalRelay-backed
CliContext — no NATS required.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from biff.cli_session import CliContext
from biff.dispatch import available_commands, dispatch
from biff.models import BiffConfig, UserSession
from biff.relay import LocalRelay


@pytest.fixture()
def relay(tmp_path: Path) -> LocalRelay:
    return LocalRelay(tmp_path)


@pytest.fixture()
def ctx(relay: LocalRelay) -> CliContext:
    return CliContext(
        relay=relay,
        config=BiffConfig(user="kai", repo_name="test"),
        session_key="kai:abc12345",
        user="kai",
        tty="abc12345",
        tty_name="tty1",
    )


class TestAvailableCommands:
    def test_lists_all_commands(self) -> None:
        cmds = available_commands()
        assert "who" in cmds
        assert "write" in cmds
        assert "read" in cmds
        assert "plan" in cmds
        assert "finger" in cmds
        assert "last" in cmds
        assert "wall" in cmds
        assert "mesg" in cmds
        assert "tty" in cmds
        assert "status" in cmds
        assert len(cmds) == 10

    def test_sorted(self) -> None:
        cmds = available_commands()
        assert cmds == sorted(cmds)


class TestDispatchBasics:
    @pytest.mark.anyio()
    async def test_empty_line(self, ctx: CliContext) -> None:
        result = await dispatch("", ctx)
        assert result is not None
        assert result.text == ""

    @pytest.mark.anyio()
    async def test_whitespace_only(self, ctx: CliContext) -> None:
        result = await dispatch("   ", ctx)
        assert result is not None
        assert result.text == ""

    @pytest.mark.anyio()
    async def test_exit(self, ctx: CliContext) -> None:
        result = await dispatch("exit", ctx)
        assert result is None

    @pytest.mark.anyio()
    async def test_quit(self, ctx: CliContext) -> None:
        result = await dispatch("quit", ctx)
        assert result is None

    @pytest.mark.anyio()
    async def test_exit_case_insensitive(self, ctx: CliContext) -> None:
        assert await dispatch("EXIT", ctx) is None
        assert await dispatch("Quit", ctx) is None

    @pytest.mark.anyio()
    async def test_unknown_command(self, ctx: CliContext) -> None:
        result = await dispatch("foobar", ctx)
        assert result is not None
        assert result.error
        assert "Unknown command" in result.text

    @pytest.mark.anyio()
    async def test_case_insensitive(self, ctx: CliContext) -> None:
        result = await dispatch("WHO", ctx)
        assert result is not None
        assert not result.error


class TestDispatchCommands:
    @pytest.mark.anyio()
    async def test_who_empty(self, ctx: CliContext) -> None:
        result = await dispatch("who", ctx)
        assert result is not None
        assert "No sessions" in result.text

    @pytest.mark.anyio()
    async def test_who_with_session(self, ctx: CliContext) -> None:
        await ctx.relay.update_session(
            UserSession(user="kai", tty="abc12345", tty_name="tty1")
        )
        result = await dispatch("who", ctx)
        assert result is not None
        assert "kai" in result.text

    @pytest.mark.anyio()
    async def test_plan(self, ctx: CliContext) -> None:
        result = await dispatch('plan "working on tests"', ctx)
        assert result is not None
        assert "working on tests" in result.text

    @pytest.mark.anyio()
    async def test_plan_unquoted(self, ctx: CliContext) -> None:
        result = await dispatch("plan fixing the bug", ctx)
        assert result is not None
        assert "fixing the bug" in result.text

    @pytest.mark.anyio()
    async def test_write_and_read(self, ctx: CliContext) -> None:
        result = await dispatch('write @kai "hello there"', ctx)
        assert result is not None
        assert "sent" in result.text.lower()

        result = await dispatch("read", ctx)
        assert result is not None
        assert "hello" in result.text

    @pytest.mark.anyio()
    async def test_write_missing_args(self, ctx: CliContext) -> None:
        result = await dispatch("write", ctx)
        assert result is not None
        assert result.error

    @pytest.mark.anyio()
    async def test_finger_missing_args(self, ctx: CliContext) -> None:
        result = await dispatch("finger", ctx)
        assert result is not None
        assert result.error

    @pytest.mark.anyio()
    async def test_mesg(self, ctx: CliContext) -> None:
        # Register session first so mesg has something to update
        await ctx.relay.update_session(
            UserSession(user="kai", tty="abc12345", tty_name="tty1")
        )
        result = await dispatch("mesg off", ctx)
        assert result is not None
        assert "n" in result.text

    @pytest.mark.anyio()
    async def test_tty(self, ctx: CliContext) -> None:
        result = await dispatch("tty dev", ctx)
        assert result is not None
        assert "dev" in result.text

    @pytest.mark.anyio()
    async def test_status(self, ctx: CliContext) -> None:
        result = await dispatch("status", ctx)
        assert result is not None
        assert "biff" in result.text

    @pytest.mark.anyio()
    async def test_wall_post_and_read(self, ctx: CliContext) -> None:
        result = await dispatch('wall "release freeze" 1h', ctx)
        assert result is not None
        assert "release freeze" in result.text

        result = await dispatch("wall", ctx)
        assert result is not None
        assert "release freeze" in result.text

    @pytest.mark.anyio()
    async def test_wall_clear(self, ctx: CliContext) -> None:
        await dispatch('wall "test" 1h', ctx)
        result = await dispatch("wall --clear", ctx)
        assert result is not None
        assert "cleared" in result.text.lower()

    @pytest.mark.anyio()
    async def test_last(self, ctx: CliContext) -> None:
        result = await dispatch("last", ctx)
        assert result is not None
        # LocalRelay returns empty wtmp
        assert "No session history" in result.text

    @pytest.mark.anyio()
    async def test_shlex_quoted_string(self, ctx: CliContext) -> None:
        result = await dispatch("plan 'multi word plan'", ctx)
        assert result is not None
        assert "multi word plan" in result.text

    @pytest.mark.anyio()
    async def test_wall_extra_args(self, ctx: CliContext) -> None:
        result = await dispatch("wall msg dur extra", ctx)
        assert result is not None
        assert result.error
        assert "Usage" in result.text

    @pytest.mark.anyio()
    async def test_wall_clear_extra_args(self, ctx: CliContext) -> None:
        result = await dispatch("wall --clear extra", ctx)
        assert result is not None
        assert result.error
        assert "Usage" in result.text

    @pytest.mark.anyio()
    async def test_tty_extra_args(self, ctx: CliContext) -> None:
        result = await dispatch("tty a b", ctx)
        assert result is not None
        assert result.error
        assert "Usage" in result.text

    @pytest.mark.anyio()
    async def test_bad_quotes(self, ctx: CliContext) -> None:
        result = await dispatch('plan "unterminated', ctx)
        assert result is not None
        assert result.error
        assert "Parse error" in result.text
