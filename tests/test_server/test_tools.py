"""Tests for individual biff MCP tools.

Each tool is tested by calling its underlying function directly via
the registered closure, verifying it reads/writes state correctly.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from fastmcp.tools.function_tool import FunctionTool

from biff._formatting import TABLE_WIDTH, visible_width
from biff.chunking import chunk_message
from biff.formatting import _NO_PRINTABLE_TEXT
from biff.models import BiffConfig, Message, UserSession
from biff.server.app import create_server
from biff.server.state import ServerState, create_state

if TYPE_CHECKING:
    from fastmcp import FastMCP

_TEST_REPO = "_test-server"

# Deterministic TTYs matching conftest fixtures
_KAI_TTY = "tty1"
_ERIC_TTY = "tty2"


def _create_mcp(state: ServerState) -> FastMCP[ServerState]:
    """Create a fully configured MCP server for testing."""
    return create_server(state)


async def _get_tool_fn(state: ServerState, tool_name: str):
    """Get the callable for a registered tool by name."""
    mcp = _create_mcp(state)
    tool = await mcp.get_tool(tool_name)
    assert tool is not None
    assert isinstance(tool, FunctionTool)
    return tool.fn


class TestBiffToggleTool:
    async def test_disable_messages(self, state: ServerState) -> None:
        fn = await _get_tool_fn(state, "mesg")
        result = await fn(enabled=False)
        assert "is n" in result
        session = await state.relay.get_session(state.session_key)
        assert session is not None
        assert session.biff_enabled is False

    async def test_enable_messages(self, state: ServerState) -> None:
        await state.relay.update_session(
            UserSession(user="kai", tty=_KAI_TTY, biff_enabled=False)
        )
        fn = await _get_tool_fn(state, "mesg")
        result = await fn(enabled=True)
        assert "is y" in result
        session = await state.relay.get_session(state.session_key)
        assert session is not None
        assert session.biff_enabled is True

    async def test_creates_session_if_missing(self, state: ServerState) -> None:
        assert await state.relay.get_session(state.session_key) is None
        fn = await _get_tool_fn(state, "mesg")
        await fn(enabled=True)
        assert await state.relay.get_session(state.session_key) is not None

    async def test_updates_last_active(self, state: ServerState) -> None:
        old_time = datetime.now(UTC) - timedelta(seconds=300)
        await state.relay.update_session(
            UserSession(user="kai", tty=_KAI_TTY, last_active=old_time)
        )
        fn = await _get_tool_fn(state, "mesg")
        await fn(enabled=False)
        session = await state.relay.get_session(state.session_key)
        assert session is not None
        assert session.last_active > old_time


def _committed_files(root: Path) -> set[str]:
    """Files under *root* relative to it, excluding git internals (``.git``)."""
    return {
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }


def _git_init(root: Path) -> Path:
    """Initialise a real git repo so ``enable`` can resolve/deploy git hooks."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "init", "-q"],  # noqa: S607
        check=True,
        capture_output=True,
    )
    return root


def _deployed_hooks(root: Path) -> set[str]:
    """Names of biff git hooks currently present in *root*'s resolved hooks dir."""
    from biff.git_hooks import GIT_HOOKS, resolve_hooks_dir

    hooks_dir = resolve_hooks_dir(root)
    assert hooks_dir is not None
    return {name for name in GIT_HOOKS if (hooks_dir / name).is_file()}


class TestBiffEnableToggleTool:
    """The ``biff`` enable/disable tool writes the committed artifacts.

    Distinct from ``mesg`` (per-user delivery): this is the repo-policy
    toggle that DES-052 keeps equivalent across the CLI and MCP surfaces.
    """

    async def test_enable_writes_marker_ci_and_hooks(
        self, config: BiffConfig, tmp_path: Path
    ) -> None:
        from biff.git_hooks import GIT_HOOKS

        repo = _git_init(tmp_path / "repo")
        state = create_state(config, tmp_path / "data", repo_root=repo)
        fn = await _get_tool_fn(state, "biff")

        result = await fn(action="enable")

        assert "enabled" in result
        assert (repo / ".punt-labs" / "biff" / "enabled").is_file()
        assert (repo / ".github" / "workflows" / "biff-notify.yml").is_file()
        # Enable fully activates the clone: the local git hooks land too.
        assert _deployed_hooks(repo) == set(GIT_HOOKS)

    async def test_disable_removes_marker_ci_and_hooks(
        self, config: BiffConfig, tmp_path: Path
    ) -> None:
        repo = _git_init(tmp_path / "repo")
        state = create_state(config, tmp_path / "data", repo_root=repo)
        fn = await _get_tool_fn(state, "biff")

        await fn(action="enable")
        result = await fn(action="disable")

        assert "disabled" in result
        assert not (repo / ".punt-labs" / "biff" / "enabled").exists()
        assert not (repo / ".github" / "workflows" / "biff-notify.yml").exists()
        assert _deployed_hooks(repo) == set()

    async def test_no_repo(self, config: BiffConfig, tmp_path: Path) -> None:
        state = create_state(config, tmp_path / "data", repo_root=None)
        fn = await _get_tool_fn(state, "biff")
        result = await fn(action="enable")
        assert "not in a git repository" in result.lower()

    async def test_enable_unresolvable_hooks_returns_notice_not_success(
        self, config: BiffConfig, tmp_path: Path
    ) -> None:
        """A non-git repo_root → hooks unresolvable → NOTICE, no marker, no success."""
        from biff.git_hooks import HOOKS_DIR_UNRESOLVED_NOTICE

        repo = tmp_path / "not-a-repo"  # exists in state but never `git init`ed
        repo.mkdir()
        state = create_state(config, tmp_path / "data", repo_root=repo)
        fn = await _get_tool_fn(state, "biff")

        result = await fn(action="enable")

        assert result == HOOKS_DIR_UNRESOLVED_NOTICE
        assert "enabled" not in result
        assert not (repo / ".punt-labs" / "biff" / "enabled").exists()

    async def test_enable_unresolvable_output_matches_cli(
        self, config: BiffConfig, tmp_path: Path
    ) -> None:
        """CLI and MCP emit the identical NOTICE on the unresolvable-hooks path."""
        from unittest.mock import patch

        from typer.testing import CliRunner

        from biff.__main__ import app

        mcp_repo = tmp_path / "mcp"  # not a git repo
        cli_repo = tmp_path / "cli"  # not a git repo
        mcp_repo.mkdir()
        cli_repo.mkdir()

        state = create_state(config, tmp_path / "data", repo_root=mcp_repo)
        fn = await _get_tool_fn(state, "biff")
        mcp_result = await fn(action="enable")

        with patch("biff.__main__.find_git_root", return_value=cli_repo):
            cli_result = CliRunner().invoke(app, ["enable"])

        assert cli_result.exit_code != 0
        assert mcp_result in cli_result.output  # identical NOTICE text

    async def test_mcp_and_cli_enable_are_equivalent(
        self, config: BiffConfig, tmp_path: Path
    ) -> None:
        """`/biff enable` (MCP) and `biff enable` (CLI) produce the same result."""
        from unittest.mock import patch

        from typer.testing import CliRunner

        from biff.__main__ import app
        from biff.git_hooks import GIT_HOOKS

        mcp_repo = _git_init(tmp_path / "mcp_repo")
        cli_repo = _git_init(tmp_path / "cli_repo")

        # MCP surface.
        state = create_state(config, tmp_path / "data", repo_root=mcp_repo)
        fn = await _get_tool_fn(state, "biff")
        await fn(action="enable")

        # CLI surface.
        with patch("biff.__main__.find_git_root", return_value=cli_repo):
            result = CliRunner().invoke(app, ["enable"])
        assert result.exit_code == 0

        # Same committed files on both surfaces...
        assert _committed_files(mcp_repo) == _committed_files(cli_repo)
        assert _committed_files(mcp_repo) == {
            ".punt-labs/biff/enabled",
            ".github/workflows/biff-notify.yml",
        }
        # ...and the same fully-active local git hooks on both.
        assert _deployed_hooks(mcp_repo) == _deployed_hooks(cli_repo)
        assert _deployed_hooks(mcp_repo) == set(GIT_HOOKS)


class TestFingerTool:
    async def test_unknown_user(self, state: ServerState) -> None:
        fn = await _get_tool_fn(state, "finger")
        result = await fn(user="nobody")
        assert "Never logged in" in result

    async def test_shows_plan(self, state: ServerState) -> None:
        await state.relay.update_session(
            UserSession(user="eric", tty=_ERIC_TTY, plan="refactoring auth")
        )
        fn = await _get_tool_fn(state, "finger")
        result = await fn(user="eric")
        assert "refactoring auth" in result
        assert "Login: eric" in result

    async def test_shows_availability(self, state: ServerState) -> None:
        await state.relay.update_session(
            UserSession(user="eric", tty=_ERIC_TTY, biff_enabled=False)
        )
        fn = await _get_tool_fn(state, "finger")
        result = await fn(user="eric")
        assert "Messages: off" in result

    async def test_strips_at_prefix(self, state: ServerState) -> None:
        await state.relay.update_session(
            UserSession(user="eric", tty=_ERIC_TTY, plan="coding")
        )
        fn = await _get_tool_fn(state, "finger")
        result = await fn(user="@eric")
        assert "coding" in result
        assert "Login: eric" in result

    async def test_shows_display_name(self, state: ServerState) -> None:
        await state.relay.update_session(
            UserSession(
                user="eric",
                tty=_ERIC_TTY,
                display_name="Eric Alvarez",
                plan="debugging",
            )
        )
        fn = await _get_tool_fn(state, "finger")
        result = await fn(user="eric")
        assert "Name: Eric Alvarez" in result
        assert "Login: eric" in result
        assert "Messages: on" in result

    async def test_omits_name_when_empty(self, state: ServerState) -> None:
        await state.relay.update_session(
            UserSession(user="eric", tty=_ERIC_TTY, plan="coding")
        )
        fn = await _get_tool_fn(state, "finger")
        result = await fn(user="eric")
        assert "Name:" not in result
        assert "Messages: on" in result

    async def test_targeted_finger(self, state: ServerState) -> None:
        """@user:tty shows a specific session."""
        await state.relay.update_session(
            UserSession(user="eric", tty=_ERIC_TTY, plan="coding")
        )
        fn = await _get_tool_fn(state, "finger")
        result = await fn(user=f"eric:{_ERIC_TTY}")
        assert "coding" in result
        assert "Login: eric" in result

    async def test_targeted_finger_missing_tty(self, state: ServerState) -> None:
        """@user:tty with unknown tty reports no session."""
        fn = await _get_tool_fn(state, "finger")
        result = await fn(user="eric:unknown")
        assert "No session on tty unknown" in result

    async def test_shows_host_and_dir(self, state: ServerState) -> None:
        await state.relay.update_session(
            UserSession(
                user="eric",
                tty=_ERIC_TTY,
                hostname="dev-box",
                pwd="/home/eric/project",
                plan="coding",
            )
        )
        fn = await _get_tool_fn(state, "finger")
        result = await fn(user="eric")
        assert "Host: dev-box" in result
        assert "Dir: /home/eric/project" in result

    async def test_multi_tty_shows_header_once(self, state: ServerState) -> None:
        """Multiple TTYs show Login/Name once, not per-TTY."""
        await state.relay.update_session(
            UserSession(
                user="eric",
                tty="tty1",
                display_name="Eric Alvarez",
                plan="coding",
            )
        )
        await state.relay.update_session(
            UserSession(
                user="eric",
                tty="tty2",
                display_name="Eric Alvarez",
                plan="reviewing",
            )
        )
        fn = await _get_tool_fn(state, "finger")
        result = await fn(user="eric")
        assert result.count("Login: eric") == 1
        assert result.count("Name: Eric Alvarez") == 1
        assert "coding" in result
        assert "reviewing" in result


class TestWhoTool:
    async def test_always_includes_self(self, state: ServerState) -> None:
        fn = await _get_tool_fn(state, "who")
        result = await fn()
        assert "kai" in result

    async def test_lists_users(self, state: ServerState) -> None:
        await state.relay.update_session(
            UserSession(user="kai", tty=_KAI_TTY, plan="coding")
        )
        await state.relay.update_session(
            UserSession(user="eric", tty=_ERIC_TTY, plan="reviewing")
        )
        fn = await _get_tool_fn(state, "who")
        result = await fn()
        assert "kai" in result
        assert "eric" in result

    async def test_shows_idle_time(self, state: ServerState) -> None:
        # Within the liveness window (60s of headroom to the 120s cutoff) so
        # the session is shown and idle renders as "1m".
        old_time = datetime.now(UTC) - timedelta(seconds=60)
        await state.relay.update_session(
            UserSession(
                user="eric", tty=_ERIC_TTY, plan="reviewing", last_active=old_time
            )
        )
        fn = await _get_tool_fn(state, "who")
        result = await fn()
        assert "eric" in result
        assert "1m" in result

    async def test_hides_dead_sessions(self, state: ServerState) -> None:
        """Sessions past the liveness window are dropped from the main table
        but still surfaced, unnamed, in the dead-session footnote
        (DES-057) once they exceed the wider DEAD_REPORT_SECONDS threshold."""
        old_time = datetime.now(UTC) - timedelta(days=2)
        recent_time = datetime.now(UTC) - timedelta(seconds=30)
        await state.relay.update_session(
            UserSession(user="old", tty="tty0", last_active=old_time, plan="vacation")
        )
        await state.relay.update_session(
            UserSession(
                user="recent", tty="tty0", last_active=recent_time, plan="coding"
            )
        )
        fn = await _get_tool_fn(state, "who")
        result = await fn()
        assert "recent" in result
        assert "old" not in result
        assert "stopped responding (last seen 2d)" in result

    async def test_sorted_by_idle_time(self, state: ServerState) -> None:
        now = datetime.now(UTC)
        # Both within the liveness window so both are shown.
        await state.relay.update_session(
            UserSession(
                user="zara",
                tty="tty0",
                plan="testing",
                last_active=now - timedelta(seconds=60),
            )
        )
        await state.relay.update_session(
            UserSession(
                user="alice",
                tty="tty0",
                plan="coding",
                last_active=now,
            )
        )
        fn = await _get_tool_fn(state, "who")
        result = await fn()
        # Most recently active first
        assert result.index("alice") < result.index("zara")

    async def test_name_includes_tty(self, state: ServerState) -> None:
        await state.relay.update_session(
            UserSession(user="kai", tty=_KAI_TTY, plan="coding")
        )
        fn = await _get_tool_fn(state, "who")
        result = await fn()
        assert f"kai:{_KAI_TTY}" in result

    async def test_shows_host_column(self, state: ServerState) -> None:
        await state.relay.update_session(
            UserSession(
                user="kai",
                tty=_KAI_TTY,
                hostname="dev-box",
                pwd="/home/kai",
                plan="coding",
            )
        )
        fn = await _get_tool_fn(state, "who")
        result = await fn()
        assert "HOST" in result
        assert "dev-box" in result


class TestPlanTool:
    async def test_sets_plan(self, state: ServerState) -> None:
        fn = await _get_tool_fn(state, "plan")
        result = await fn(message="refactoring auth")
        assert "refactoring auth" in result
        session = await state.relay.get_session(state.session_key)
        assert session is not None
        assert session.plan == "refactoring auth"

    async def test_sets_plan_source_manual(self, state: ServerState) -> None:
        fn = await _get_tool_fn(state, "plan")
        await fn(message="refactoring auth")
        session = await state.relay.get_session(state.session_key)
        assert session is not None
        assert session.plan_source == "manual"

    async def test_source_auto_sets_plan_source(self, state: ServerState) -> None:
        """Hooks pass source='auto' to mark plans as overwritable."""
        fn = await _get_tool_fn(state, "plan")
        await fn(message="→ feature-branch", source="auto")
        session = await state.relay.get_session(state.session_key)
        assert session is not None
        assert session.plan == "→ feature-branch"
        assert session.plan_source == "auto"

    async def test_overwrites_auto_plan_source(self, state: ServerState) -> None:
        """Manual /plan overwrites an auto plan_source from a git hook."""
        await state.relay.update_session(
            UserSession(
                user="kai", tty=_KAI_TTY, plan="→ feature-branch", plan_source="auto"
            )
        )
        fn = await _get_tool_fn(state, "plan")
        await fn(message="deep refactoring")
        session = await state.relay.get_session(state.session_key)
        assert session is not None
        assert session.plan == "deep refactoring"
        assert session.plan_source == "manual"

    async def test_auto_cannot_overwrite_manual(self, state: ServerState) -> None:
        """Auto plan from git hook must not overwrite a manual /plan."""
        await state.relay.update_session(
            UserSession(
                user="kai", tty=_KAI_TTY, plan="deep refactoring", plan_source="manual"
            )
        )
        fn = await _get_tool_fn(state, "plan")
        result = await fn(message="→ feature-branch", source="auto")
        assert "unchanged" in result
        session = await state.relay.get_session(state.session_key)
        assert session is not None
        assert session.plan == "deep refactoring"
        assert session.plan_source == "manual"

    async def test_auto_overwrites_auto(self, state: ServerState) -> None:
        """Auto plan can overwrite another auto plan (branch switch)."""
        await state.relay.update_session(
            UserSession(
                user="kai", tty=_KAI_TTY, plan="→ old-branch", plan_source="auto"
            )
        )
        fn = await _get_tool_fn(state, "plan")
        await fn(message="→ new-branch", source="auto")
        session = await state.relay.get_session(state.session_key)
        assert session is not None
        assert session.plan == "→ new-branch"
        assert session.plan_source == "auto"

    async def test_auto_clears_empty_manual(self, state: ServerState) -> None:
        """Auto plan can replace an empty manual plan (session start)."""
        await state.relay.update_session(
            UserSession(user="kai", tty=_KAI_TTY, plan="", plan_source="manual")
        )
        fn = await _get_tool_fn(state, "plan")
        await fn(message="→ feature-branch", source="auto")
        session = await state.relay.get_session(state.session_key)
        assert session is not None
        assert session.plan == "→ feature-branch"
        assert session.plan_source == "auto"

    async def test_updates_existing_plan(self, state: ServerState) -> None:
        await state.relay.update_session(
            UserSession(user="kai", tty=_KAI_TTY, plan="old plan")
        )
        fn = await _get_tool_fn(state, "plan")
        await fn(message="new plan")
        session = await state.relay.get_session(state.session_key)
        assert session is not None
        assert session.plan == "new plan"

    async def test_creates_session_if_missing(self, state: ServerState) -> None:
        assert await state.relay.get_session(state.session_key) is None
        fn = await _get_tool_fn(state, "plan")
        await fn(message="starting fresh")
        session = await state.relay.get_session(state.session_key)
        assert session is not None
        assert session.plan == "starting fresh"

    async def test_updates_last_active(self, state: ServerState) -> None:
        old_time = datetime.now(UTC) - timedelta(seconds=300)
        await state.relay.update_session(
            UserSession(user="kai", tty=_KAI_TTY, last_active=old_time)
        )
        fn = await _get_tool_fn(state, "plan")
        await fn(message="new work")
        session = await state.relay.get_session(state.session_key)
        assert session is not None
        assert session.last_active > old_time

    async def test_cjk_plan_wraps_within_the_table_width(
        self, state: ServerState
    ) -> None:
        # The MCP /plan confirmation shares format_talk_echo's wrap
        # treatment with the CLI command — a CJK-heavy plan must wrap here
        # too, not just at the CLI.
        fn = await _get_tool_fn(state, "plan")
        message = "这是一段很长的中文文本用来测试自动换行是否正常工作" * 3
        result = await fn(message=message)
        lines = result.splitlines()
        assert len(lines) > 2
        for line in lines:
            assert visible_width(line) <= TABLE_WIDTH

    async def test_control_only_message_shows_fallback(
        self, state: ServerState
    ) -> None:
        fn = await _get_tool_fn(state, "plan")
        result = await fn(message="\x00\x1b\x07")
        assert _NO_PRINTABLE_TEXT in result

    async def test_unchanged_manual_plan_wraps_within_the_table_width(
        self, state: ServerState
    ) -> None:
        # The "Plan unchanged (manual)" no-op echoes the existing session
        # plan (biff.formatting.format_talk_echo) and must wrap it the same
        # way as a fresh set.
        await state.relay.update_session(
            UserSession(
                user="kai",
                tty=_KAI_TTY,
                plan="这是一段很长的中文文本用来测试自动换行是否正常工作" * 3,
                plan_source="manual",
            )
        )
        fn = await _get_tool_fn(state, "plan")
        result = await fn(message="→ feature-branch", source="auto")
        assert "unchanged" in result
        lines = result.splitlines()
        assert len(lines) > 2
        for line in lines:
            assert visible_width(line) <= TABLE_WIDTH


class TestSendMessageTool:
    async def test_sends_targeted_message(self, state: ServerState) -> None:
        await state.relay.update_session(UserSession(user="eric", tty=_ERIC_TTY))
        fn = await _get_tool_fn(state, "write")
        result = await fn(to=f"eric:{_ERIC_TTY}", message="hey, PR is ready")
        assert "eric" in result
        unread = await state.relay.fetch(f"eric:{_ERIC_TTY}")
        assert len(unread) == 1
        assert unread[0].from_user == "kai"
        assert unread[0].body == "hey, PR is ready"

    async def test_broadcast_delivers_to_user_mailbox(self, state: ServerState) -> None:
        """Broadcast delivery goes to user mailbox, not per-TTY."""
        await state.relay.update_session(UserSession(user="eric", tty=_ERIC_TTY))
        fn = await _get_tool_fn(state, "write")
        result = await fn(to="eric", message="hello")
        assert "eric" in result
        unread = await state.relay.fetch_user_inbox("eric")
        assert len(unread) == 1

    async def test_strips_at_prefix(self, state: ServerState) -> None:
        await state.relay.update_session(UserSession(user="eric", tty=_ERIC_TTY))
        fn = await _get_tool_fn(state, "write")
        await fn(to=f"@eric:{_ERIC_TTY}", message="hello")
        unread = await state.relay.fetch(f"eric:{_ERIC_TTY}")
        assert len(unread) == 1
        assert unread[0].to_user == f"eric:{_ERIC_TTY}"

    async def test_delivers_when_biff_off(self, state: ServerState) -> None:
        await state.relay.update_session(
            UserSession(user="eric", tty=_ERIC_TTY, biff_enabled=False)
        )
        fn = await _get_tool_fn(state, "write")
        result = await fn(to=f"eric:{_ERIC_TTY}", message="urgent fix needed")
        assert "eric" in result
        unread = await state.relay.fetch(f"eric:{_ERIC_TTY}")
        assert len(unread) == 1

    async def test_multiple_messages(self, state: ServerState) -> None:
        await state.relay.update_session(UserSession(user="eric", tty=_ERIC_TTY))
        fn = await _get_tool_fn(state, "write")
        await fn(to=f"eric:{_ERIC_TTY}", message="first")
        await fn(to=f"eric:{_ERIC_TTY}", message="second")
        unread = await state.relay.fetch(f"eric:{_ERIC_TTY}")
        assert len(unread) == 2

    async def test_targeted_nonexistent_returns_error(self, state: ServerState) -> None:
        fn = await _get_tool_fn(state, "write")
        result = await fn(to="nobody:tty99", message="hello")
        assert "not found" in result

    async def test_targeted_wrong_tty_suggests_who(self, state: ServerState) -> None:
        await state.relay.update_session(UserSession(user="eric", tty=_ERIC_TTY))
        fn = await _get_tool_fn(state, "write")
        result = await fn(to="eric:fakeTty", message="hello")
        assert "Run /who to find their current address" in result

    async def test_write_bare_nonexistent_user_delivers(
        self, state: ServerState
    ) -> None:
        # Bare user without @ now delivers like @user (no validation gate)
        fn = await _get_tool_fn(state, "write")
        result = await fn(to="nobody", message="hello")
        assert "nobody" in result
        unread = await state.relay.fetch_user_inbox("nobody")
        assert len(unread) == 1

    async def test_write_bare_existing_user_succeeds(self, state: ServerState) -> None:
        await state.relay.update_session(UserSession(user="eric", tty=_ERIC_TTY))
        fn = await _get_tool_fn(state, "write")
        result = await fn(to="eric", message="hello")
        assert "eric" in result
        unread = await state.relay.fetch_user_inbox("eric")
        assert len(unread) == 1

    async def test_broadcast_to_offline_user_succeeds(self, state: ServerState) -> None:
        # @user with @ prefix allows offline delivery — no session needed
        fn = await _get_tool_fn(state, "write")
        result = await fn(to="@offlineuser", message="offline msg")
        assert "offlineuser" in result
        assert "not found" not in result
        unread = await state.relay.fetch_user_inbox("offlineuser")
        assert len(unread) == 1

    async def test_delivery_awaited_not_fire_and_forget(
        self, state: ServerState
    ) -> None:
        """biff-0px: the message is in the recipient's inbox before write() returns.

        The prior fire-and-forget design could return "Message sent" before
        the background delivery task had even run — a caller inspecting the
        inbox immediately after write() returned could observe it still
        empty. Delivery is now awaited, so this is no longer racy.
        """
        await state.relay.update_session(UserSession(user="eric", tty=_ERIC_TTY))
        fn = await _get_tool_fn(state, "write")
        await fn(to=f"eric:{_ERIC_TTY}", message="no race")
        unread = await state.relay.fetch(f"eric:{_ERIC_TTY}")
        assert len(unread) == 1

    async def test_recovers_on_retry_after_one_failure(
        self, state: ServerState
    ) -> None:
        """A single transient failure is recovered by the retry-once.

        Mirrors the observed recovery pattern (biff-brn): every
        session-reported transport-error occurrence cleared on the very
        next attempt.
        """
        await state.relay.update_session(UserSession(user="eric", tty=_ERIC_TTY))
        real_deliver = state.relay.deliver
        calls = {"n": 0}

        async def _flaky_deliver(*args: object, **kwargs: object) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                msg = "nats: timeout"
                raise TimeoutError(msg)
            await real_deliver(*args, **kwargs)  # type: ignore[arg-type]

        state.relay.deliver = _flaky_deliver  # type: ignore[method-assign]
        fn = await _get_tool_fn(state, "write")
        result = await fn(to=f"eric:{_ERIC_TTY}", message="retried ok")
        assert "Message sent" in result
        assert calls["n"] == 2
        unread = await state.relay.fetch(f"eric:{_ERIC_TTY}")
        assert len(unread) == 1

    async def test_reports_failure_distinctly_after_two_failures(
        self, state: ServerState
    ) -> None:
        """biff-0px/biff-brn: a persistent failure must not read as success.

        The prior fire-and-forget design would have returned "Message
        sent" here regardless — the failure reached only a background log
        line. It must now be visibly distinguishable from success, and the
        message must not silently appear delivered when it was not.
        """
        await state.relay.update_session(UserSession(user="eric", tty=_ERIC_TTY))

        async def _always_fails(*_args: object, **_kwargs: object) -> None:
            msg = "nats: timeout"
            raise TimeoutError(msg)

        state.relay.deliver = _always_fails  # type: ignore[method-assign]
        fn = await _get_tool_fn(state, "write")
        result = await fn(to=f"eric:{_ERIC_TTY}", message="will not arrive")
        assert "Message sent" not in result
        assert "Could not deliver" in result
        assert "not confirmed sent" in result.lower()
        unread = await state.relay.fetch(f"eric:{_ERIC_TTY}")
        assert len(unread) == 0

    async def test_partial_chunk_failure_retries_only_undelivered_chunks(
        self, state: ServerState
    ) -> None:
        """A failure partway through a multi-chunk message doesn't redeliver
        the chunks that already succeeded.
        """
        await state.relay.update_session(UserSession(user="eric", tty=_ERIC_TTY))
        real_deliver = state.relay.deliver
        delivered_bodies: list[str] = []
        calls = {"n": 0}

        async def _fail_on_second_call(msg: object, **kwargs: object) -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                err = "nats: timeout"
                raise TimeoutError(err)
            delivered_bodies.append(msg.body)  # type: ignore[attr-defined]
            await real_deliver(msg, **kwargs)  # type: ignore[arg-type]

        state.relay.deliver = _fail_on_second_call  # type: ignore[assignment]
        fn = await _get_tool_fn(state, "write")
        long_message = " ".join(f"word{i}" for i in range(400))  # forces 3+ chunks
        expected_chunks = chunk_message(long_message)
        assert len(expected_chunks) >= 3, "test needs 3+ chunks to exercise the retry"
        result = await fn(to=f"eric:{_ERIC_TTY}", message=long_message)
        assert "Message sent" in result
        # Chunk 1 delivered once (call 1), chunk 2 failed (call 2) then
        # delivered on retry (call 3), chunk 3 delivered once (call 4) —
        # pins the resume semantics exactly: each chunk delivered exactly
        # once, in order, never a redelivery of chunk 1.
        assert delivered_bodies == expected_chunks


class TestCheckMessagesTool:
    async def test_no_messages(self, state: ServerState) -> None:
        fn = await _get_tool_fn(state, "read_messages")
        result = await fn()
        assert "No new messages" in result

    async def test_recovers_on_retry_after_one_failure(
        self, state: ServerState
    ) -> None:
        """biff-brn: a single transient fetch failure is recovered by the
        code-level retry-once, matching the observed recovery pattern —
        every session-reported occurrence cleared on the very next attempt.
        """
        real_fetch = state.relay.fetch
        calls = {"n": 0}

        async def _flaky_fetch(*args: object, **kwargs: object) -> list[Message]:
            calls["n"] += 1
            if calls["n"] == 1:
                msg = "nats: timeout"
                raise TimeoutError(msg)
            return await real_fetch(*args, **kwargs)  # type: ignore[arg-type]

        state.relay.fetch = _flaky_fetch  # type: ignore[method-assign]
        fn = await _get_tool_fn(state, "read_messages")
        result = await fn()
        assert "Could not check" not in result
        # >= 2: retry happened. The exact total also includes
        # refresh_read_messages's own get_unread_summary()->fetch() call
        # after a successful retry, which is unrelated to the retry logic
        # under test.
        assert calls["n"] >= 2

    async def test_reports_failure_distinctly_after_two_failures(
        self, state: ServerState
    ) -> None:
        """biff-brn: a persistent transport failure must not render as, or
        be silently treated as, a confirmed-empty inbox. The tool itself
        (not just the /biff:read prompt) must return a distinguishable
        result rather than raising, so a caller cannot swallow an
        unhandled exception into silence.
        """

        async def _always_fails(*_args: object, **_kwargs: object) -> list[Message]:
            msg = "nats: timeout"
            raise TimeoutError(msg)

        state.relay.fetch = _always_fails  # type: ignore[method-assign]
        fn = await _get_tool_fn(state, "read_messages")
        result = await fn()
        assert "No new messages" not in result
        assert "Could not check" in result
        assert "not confirmed empty" in result.lower()

    async def test_successful_inbox_never_refetched_or_lost_on_sibling_failure(
        self, state: ServerState
    ) -> None:
        """HIGH-severity review finding, fixed here: a naive whole-batch
        retry would re-call fetch() on the tty inbox even though it
        already succeeded. On NatsRelay, fetch() destructively acks
        (deletes) messages from the stream as a side effect of a
        successful pull — a second call for the same reason a retry would
        make returns nothing, silently discarding the messages the first
        call already returned. The fix retries only the inbox that
        actually failed (fetch_user_inbox here), never the one that
        already succeeded (fetch).

        Exercises _fetch_unread_with_retry directly rather than through
        the full read_messages tool: the tool's own downstream
        refresh_read_messages() call legitimately calls fetch() again
        afterwards (for the unrelated tool-description unread count), so
        a call count taken at the read_messages level can't isolate
        whether THIS function's own retry re-fetched the succeeded inbox.
        """
        from biff.server.tools.messaging import _fetch_unread_with_retry

        real_fetch = state.relay.fetch
        fetch_calls = {"n": 0}

        async def _fetch_once_then_empty(
            *args: object, **kwargs: object
        ) -> list[Message]:
            fetch_calls["n"] += 1
            if fetch_calls["n"] > 1:
                return []  # simulates WORK_QUEUE: nothing left after the ack
            return await real_fetch(*args, **kwargs)  # type: ignore[arg-type]

        async def _user_inbox_always_fails(
            *_args: object, **_kwargs: object
        ) -> list[Message]:
            msg = "nats: timeout"
            raise TimeoutError(msg)

        await state.relay.deliver(
            Message(from_user="kai", to_user=f"kai:{_KAI_TTY}", body="tty message")
        )
        state.relay.fetch = _fetch_once_then_empty  # type: ignore[method-assign]
        state.relay.fetch_user_inbox = _user_inbox_always_fails  # type: ignore[method-assign]

        (
            (tty_unread, user_unread, comp_tty, comp_user),
            warning,
        ) = await _fetch_unread_with_retry(state)

        assert fetch_calls["n"] == 1, "the succeeded fetch must never be retried"
        assert [m.body for m in tty_unread] == ["tty message"], (
            "the already-fetched message must not be lost"
        )
        assert user_unread == []
        assert comp_tty == []
        assert comp_user == []
        assert warning is not None
        assert "your broadcast inbox" in warning

    async def test_shows_unread(self, state: ServerState, tmp_path: Path) -> None:
        # Register kai so eric can resolve the targeted address.
        await state.relay.update_session(UserSession(user="kai", tty=_KAI_TTY))
        eric_state = create_state(
            BiffConfig(user="eric", repo_name=_TEST_REPO),
            tmp_path,
            tty=_ERIC_TTY,
        )
        # Use targeted delivery to kai's session
        eric_send = await _get_tool_fn(eric_state, "write")
        await eric_send(to=f"kai:{_KAI_TTY}", message="review my PR please")

        check_fn = await _get_tool_fn(state, "read_messages")
        result = await check_fn()
        assert "FROM" in result
        assert "eric" in result
        assert "review my PR please" in result

    async def test_marks_as_read(self, state: ServerState, tmp_path: Path) -> None:
        # Register kai so eric can resolve the targeted address.
        await state.relay.update_session(UserSession(user="kai", tty=_KAI_TTY))
        eric_state = create_state(
            BiffConfig(user="eric", repo_name=_TEST_REPO),
            tmp_path,
            tty=_ERIC_TTY,
        )
        eric_send = await _get_tool_fn(eric_state, "write")
        await eric_send(to=f"kai:{_KAI_TTY}", message="hello")

        check_fn = await _get_tool_fn(state, "read_messages")
        await check_fn()

        # Second check should show no new messages
        result = await check_fn()
        assert "No new messages" in result

    async def test_multiple_senders(self, state: ServerState, tmp_path: Path) -> None:
        # Register kai so senders can resolve the targeted address.
        await state.relay.update_session(UserSession(user="kai", tty=_KAI_TTY))
        eric_state = create_state(
            BiffConfig(user="eric", repo_name=_TEST_REPO),
            tmp_path,
            tty=_ERIC_TTY,
        )
        priya_state = create_state(
            BiffConfig(user="priya", repo_name=_TEST_REPO),
            tmp_path,
            tty="tty3",
        )
        eric_send = await _get_tool_fn(eric_state, "write")
        await eric_send(to=f"kai:{_KAI_TTY}", message="from eric")
        priya_send = await _get_tool_fn(priya_state, "write")
        await priya_send(to=f"kai:{_KAI_TTY}", message="from priya")

        check_fn = await _get_tool_fn(state, "read_messages")
        result = await check_fn()
        assert "eric" in result
        assert "priya" in result
        assert "from eric" in result
        assert "from priya" in result


class TestToolInteractions:
    """Cross-tool integration tests verifying shared state."""

    async def test_plan_then_finger_shows_plan(self, state: ServerState) -> None:
        plan_fn = await _get_tool_fn(state, "plan")
        finger_fn = await _get_tool_fn(state, "finger")
        await plan_fn(message="refactoring auth")
        result = await finger_fn(user="kai")
        assert "refactoring auth" in result

    async def test_biff_off_then_finger_shows_unavailable(
        self, state: ServerState
    ) -> None:
        biff_fn = await _get_tool_fn(state, "mesg")
        finger_fn = await _get_tool_fn(state, "finger")
        await biff_fn(enabled=False)
        result = await finger_fn(user="kai")
        assert "Messages: off" in result


async def _tool_description(mcp: FastMCP[ServerState], name: str) -> str:
    """Get a tool's current description from the MCP instance."""
    tool = await mcp.get_tool(name)
    assert tool is not None
    assert tool.description is not None
    return tool.description


class TestDynamicDescriptions:
    """Verify check_messages description updates after tool calls."""

    async def test_default_description_when_no_messages(
        self, state: ServerState
    ) -> None:
        mcp = _create_mcp(state)
        desc = await _tool_description(mcp, "read_messages")
        assert desc == "Check your inbox for new messages. Marks all as read."

    async def test_description_shows_unread_after_send(
        self, state: ServerState
    ) -> None:
        mcp = _create_mcp(state)
        # eric sends kai a message (targeted delivery to kai's session)
        await state.relay.deliver(
            Message(
                from_user="eric",
                to_user=f"kai:{_KAI_TTY}",
                body="auth ready",
            )
        )
        # kai calls any tool — triggers description refresh
        plan_tool = await mcp.get_tool("plan")
        assert isinstance(plan_tool, FunctionTool)
        await plan_tool.fn(message="working")
        desc = await _tool_description(mcp, "read_messages")
        assert "1 unread" in desc

    async def test_description_reverts_after_check(self, state: ServerState) -> None:
        mcp = _create_mcp(state)
        await state.relay.deliver(
            Message(
                from_user="eric",
                to_user=f"kai:{_KAI_TTY}",
                body="hello",
            )
        )
        # Trigger refresh via plan
        plan_tool = await mcp.get_tool("plan")
        assert isinstance(plan_tool, FunctionTool)
        await plan_tool.fn(message="working")
        assert "1 unread" in await _tool_description(mcp, "read_messages")
        # Now check messages — should clear the description
        check_tool = await mcp.get_tool("read_messages")
        assert isinstance(check_tool, FunctionTool)
        await check_tool.fn()
        desc = await _tool_description(mcp, "read_messages")
        assert desc == "Check your inbox for new messages. Marks all as read."

    async def test_description_shows_multiple_senders(self, state: ServerState) -> None:
        mcp = _create_mcp(state)
        await state.relay.deliver(
            Message(
                from_user="eric",
                to_user=f"kai:{_KAI_TTY}",
                body="PR approved",
            )
        )
        await state.relay.deliver(
            Message(
                from_user="priya",
                to_user=f"kai:{_KAI_TTY}",
                body="tests pass",
            )
        )
        # Trigger via who
        who_tool = await mcp.get_tool("who")
        assert isinstance(who_tool, FunctionTool)
        await who_tool.fn()
        desc = await _tool_description(mcp, "read_messages")
        assert "2 unread" in desc

    async def test_send_message_triggers_refresh(self, state: ServerState) -> None:
        mcp = _create_mcp(state)
        # Register eric so kai can resolve the targeted address.
        await state.relay.update_session(UserSession(user="eric", tty=_ERIC_TTY))
        # Another user sends to kai first (targeted)
        await state.relay.deliver(
            Message(
                from_user="eric",
                to_user=f"kai:{_KAI_TTY}",
                body="hello",
            )
        )
        # kai sends a message — should also refresh description
        send_tool = await mcp.get_tool("write")
        assert isinstance(send_tool, FunctionTool)
        await send_tool.fn(to=f"eric:{_ERIC_TTY}", message="hey back")
        desc = await _tool_description(mcp, "read_messages")
        assert "1 unread" in desc

    async def test_finger_triggers_refresh(self, state: ServerState) -> None:
        mcp = _create_mcp(state)
        await state.relay.update_session(
            UserSession(user="eric", tty=_ERIC_TTY, plan="coding")
        )
        await state.relay.deliver(
            Message(
                from_user="eric",
                to_user=f"kai:{_KAI_TTY}",
                body="look at this",
            )
        )
        finger_tool = await mcp.get_tool("finger")
        assert isinstance(finger_tool, FunctionTool)
        await finger_tool.fn(user="eric")
        desc = await _tool_description(mcp, "read_messages")
        assert "1 unread" in desc

    async def test_biff_toggle_triggers_refresh(self, state: ServerState) -> None:
        mcp = _create_mcp(state)
        await state.relay.deliver(
            Message(
                from_user="eric",
                to_user=f"kai:{_KAI_TTY}",
                body="urgent",
            )
        )
        biff_tool = await mcp.get_tool("mesg")
        assert isinstance(biff_tool, FunctionTool)
        await biff_tool.fn(enabled=False)
        desc = await _tool_description(mcp, "read_messages")
        assert "1 unread" in desc
