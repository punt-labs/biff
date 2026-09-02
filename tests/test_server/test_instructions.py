"""Tests for the MCP server receive/poll guidance and the /biff:read command.

The server teaches the agent biff's passive/pull receive model; the read
command and the mutated tool descriptions must name the same markers
(``[TALK]`` for talk, ``unread)`` for mail) so they never drift apart.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from biff.models import BiffConfig
from biff.relay import LocalRelay
from biff.server.app import create_server
from biff.server.state import create_state
from biff.server.tools._descriptions import TALK_BASE_DESCRIPTION, _talk_description
from biff.talk_state import TalkState

_COMMANDS = Path(__file__).resolve().parents[2] / "plugin" / "commands"

if not _COMMANDS.is_dir():
    # A relocated plugin surface must fail collection, not quietly flip
    # ``_DEV_PLUGIN`` to False and turn the dev-command assertions below into
    # skips that report as a passing run.
    raise RuntimeError(f"plugin commands directory not found: {_COMMANDS}")

# The ``*-dev.md`` command files are a dev-plugin-only artifact:
# ``scripts/release-plugin.sh`` deletes them when swapping to the prod plugin
# for a release. Assertions on their existence are only valid in the dev state.
_DEV_PLUGIN = (_COMMANDS / "read-dev.md").is_file()


def _server_instructions(tmp_path: Path) -> str:
    config = BiffConfig(user="kai", repo_name="test")
    state = create_state(config, tmp_path, relay=LocalRelay(tmp_path))
    return create_server(state).instructions or ""


class TestServerInstructions:
    """The server instructions teach the agent how to stay responsive."""

    def test_teaches_passive_pull(self, tmp_path: Path) -> None:
        text = _server_instructions(tmp_path)
        assert "passive" in text.lower()
        # Unified command: "/biff:read 5m" starts polling; "/biff:read" checks now.
        assert "/biff:read 5m" in text
        assert "/biff:read 1m" in text

    def test_names_the_exact_markers(self, tmp_path: Path) -> None:
        text = _server_instructions(tmp_path)
        assert "[TALK]" in text
        assert "unread" in text

    def test_names_receive_tools(self, tmp_path: Path) -> None:
        text = _server_instructions(tmp_path)
        assert "talk_read" in text
        assert "read_messages" in text


class TestTalkDescriptionMarker:
    """``_talk_description`` emits the ``[TALK]`` marker the read command checks."""

    def _talk(self, tmp_path: Path) -> TalkState:
        return TalkState(
            relay=LocalRelay(tmp_path), user="kai", tty="t", session_key="kai:t"
        )

    def test_idle_has_no_marker(self, tmp_path: Path) -> None:
        assert _talk_description(self._talk(tmp_path)) == TALK_BASE_DESCRIPTION
        assert "[TALK]" not in TALK_BASE_DESCRIPTION

    def test_pending_invite_has_marker(self, tmp_path: Path) -> None:
        talk = self._talk(tmp_path)
        talk.receive(
            {
                "type": "invite",
                "from": "eric",
                "from_key": "eric:x",
                "body": "hi",
                "to_key": "kai:t",
            }
        )
        talk.drain_idle()  # record the pending invite
        assert _talk_description(talk).startswith("[TALK]")


class TestReadCommand:
    """The unified /biff:read: a duration starts polling; no arg checks now."""

    def test_prod_exists(self) -> None:
        assert (_COMMANDS / "read.md").is_file()

    @pytest.mark.skipif(
        not _DEV_PLUGIN,
        reason="dev commands are removed in a prod release build (release-plugin.sh)",
    )
    def test_dev_exists(self) -> None:
        assert (_COMMANDS / "read-dev.md").is_file()

    def test_prod_check_now_references_markers_and_tools(self) -> None:
        text = (_COMMANDS / "read.md").read_text()
        assert "[TALK]" in text
        assert "unread)" in text
        assert "mcp__plugin_biff_tty__talk_read" in text
        assert "mcp__plugin_biff_tty__read_messages" in text

    def test_prod_duration_form_sets_interval_and_loop(self) -> None:
        text = (_COMMANDS / "read.md").read_text()
        assert "mcp__plugin_biff_tty__set_poll_interval" in text
        assert "CronCreate" in text

    @pytest.mark.skipif(
        not _DEV_PLUGIN,
        reason="dev commands are removed in a prod release build (release-plugin.sh)",
    )
    def test_dev_routes_to_dev_plugin(self) -> None:
        text = (_COMMANDS / "read-dev.md").read_text()
        assert "[TALK]" in text
        assert "unread)" in text
        assert "mcp__plugin_biff-dev_tty__talk_read" in text
        assert "mcp__plugin_biff-dev_tty__read_messages" in text
        assert "mcp__plugin_biff-dev_tty__set_poll_interval" in text
