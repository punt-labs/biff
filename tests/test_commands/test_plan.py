"""Tests for ``biff.commands.plan``."""

from __future__ import annotations

from biff._formatting import TABLE_WIDTH, visible_width
from biff.cli_session import CliContext
from biff.commands.plan import plan
from biff.formatting import _NO_PRINTABLE_TEXT, _NO_VISIBLE_CONTENT
from biff.relay import LocalRelay


class TestPlan:
    async def test_set_plan(self, ctx: CliContext, relay: LocalRelay) -> None:
        result = await plan(ctx, "working on tests")
        assert not result.error
        assert result.text == "Plan:\n   working on tests"
        assert result.json_data == {"plan": "working on tests"}

        # Verify session was updated
        session = await relay.get_session("kai:abc12345")
        assert session is not None
        assert session.plan == "working on tests"
        assert session.plan_source == "manual"

    async def test_update_existing_plan(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        await plan(ctx, "first plan")
        result = await plan(ctx, "second plan")
        assert not result.error
        assert result.text == "Plan:\n   second plan"

        session = await relay.get_session("kai:abc12345")
        assert session is not None
        assert session.plan == "second plan"

    async def test_empty_plan(self, ctx: CliContext, relay: LocalRelay) -> None:
        result = await plan(ctx, "")
        assert not result.error
        assert result.text == "Plan:\n   "
        assert result.json_data == {"plan": ""}

        session = await relay.get_session("kai:abc12345")
        assert session is not None
        assert session.plan == ""

    async def test_creates_session_when_none_exists(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        # No session registered yet — plan should create one
        session_before = await relay.get_session("kai:abc12345")
        assert session_before is None

        result = await plan(ctx, "bootstrapped")
        assert not result.error

        session = await relay.get_session("kai:abc12345")
        assert session is not None
        assert session.plan == "bootstrapped"
        assert session.tty_name == "cli"

    async def test_cjk_plan_wraps_within_the_table_width(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        # /plan is free-form, unbounded-length user text — a CJK-heavy plan
        # renders at 2 cells/glyph and must wrap the confirmation, not
        # overflow it onto one unbounded line.
        message = "这是一段很长的中文文本用来测试自动换行是否正常工作" * 3
        result = await plan(ctx, message)
        assert not result.error
        lines = result.text.splitlines()
        assert len(lines) > 2  # "Plan:" + wrapped body lines
        for line in lines:
            assert visible_width(line) <= TABLE_WIDTH

    async def test_escape_in_message_neutralized(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        result = await plan(ctx, "clear\x1b[2Jme")
        assert not result.error
        assert "\x1b[2J" not in result.text
        assert "clear[2Jme" in result.text

    async def test_control_only_message_shows_fallback(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        # A message that is every character control/escape strips to nothing
        # under terminal_safe — the confirmation must say so explicitly
        # rather than look like a successful, empty plan was set.
        result = await plan(ctx, "\x00\x1b\x07")
        assert not result.error
        assert _NO_PRINTABLE_TEXT in result.text

    async def test_whitespace_only_message_confirmation_matches_stored_state(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        # "   " survives terminal_safe unchanged (spaces are printable), so
        # the confirmation must not claim it was "stripped" of anything — it
        # wasn't.  ``plan()`` writes "   " to the session verbatim
        # (``model_copy(update=...)`` bypasses pydantic's
        # ``str_strip_whitespace`` validator on write), but every *read* of a
        # persisted session re-validates via ``model_validate`` /
        # ``model_validate_json`` — which strips "   " down to "" — so a
        # subsequent ``/finger`` or ``/who`` shows no plan at all.  "(message
        # had no visible content)" is the accurate framing for both moments:
        # nothing survives to be seen, on the confirmation or on read-back.
        result = await plan(ctx, "   ")
        assert not result.error
        assert _NO_PRINTABLE_TEXT not in result.text
        assert _NO_VISIBLE_CONTENT in result.text

        session = await relay.get_session("kai:abc12345")
        assert session is not None
        assert session.plan == ""
