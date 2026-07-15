"""Tests for the MCP server receive/poll guidance and the /biff:poll command.

The server teaches the agent biff's passive/pull receive model; the poll
command and the mutated tool descriptions must name the same markers
(``[TALK]`` for talk, ``unread)`` for mail) so they never drift apart.
"""

from __future__ import annotations

from pathlib import Path

from biff.models import BiffConfig
from biff.relay import LocalRelay
from biff.server.app import create_server
from biff.server.state import create_state
from biff.server.tools._descriptions import TALK_BASE_DESCRIPTION, _talk_description
from biff.talk_state import TalkState

_COMMANDS = Path(__file__).resolve().parents[2] / "commands"


def _server_instructions(tmp_path: Path) -> str:
    config = BiffConfig(user="kai", repo_name="test")
    state = create_state(config, tmp_path, relay=LocalRelay(tmp_path))
    return create_server(state).instructions or ""


class TestServerInstructions:
    """The server instructions teach the agent how to stay responsive."""

    def test_teaches_passive_pull(self, tmp_path: Path) -> None:
        text = _server_instructions(tmp_path)
        assert "passive" in text.lower()
        # Unified command: "/biff:poll 5m" starts polling; "/biff:poll" checks now.
        assert "/biff:poll 5m" in text
        assert "/biff:poll 1m" in text

    def test_names_the_exact_markers(self, tmp_path: Path) -> None:
        text = _server_instructions(tmp_path)
        assert "[TALK]" in text
        assert "unread" in text

    def test_names_receive_tools(self, tmp_path: Path) -> None:
        text = _server_instructions(tmp_path)
        assert "talk_read" in text
        assert "read_messages" in text


class TestTalkDescriptionMarker:
    """``_talk_description`` emits the ``[TALK]`` marker the poll command checks."""

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


class TestPollCommand:
    """The unified /biff:poll: a duration starts polling; no arg checks now."""

    def test_prod_and_dev_exist(self) -> None:
        assert (_COMMANDS / "poll.md").is_file()
        assert (_COMMANDS / "poll-dev.md").is_file()

    def test_prod_check_now_references_markers_and_tools(self) -> None:
        text = (_COMMANDS / "poll.md").read_text()
        assert "[TALK]" in text
        assert "unread)" in text
        assert "mcp__plugin_biff_tty__talk_read" in text
        assert "mcp__plugin_biff_tty__read_messages" in text

    def test_prod_duration_form_sets_interval_and_loop(self) -> None:
        text = (_COMMANDS / "poll.md").read_text()
        assert "mcp__plugin_biff_tty__set_poll_interval" in text
        assert "CronCreate" in text
        # The recurring loop runs /biff:poll with NO argument (no re-schedule).
        assert "with NO" in text

    def test_dev_routes_to_dev_plugin(self) -> None:
        text = (_COMMANDS / "poll-dev.md").read_text()
        assert "[TALK]" in text
        assert "unread)" in text
        assert "mcp__plugin_biff-dev_tty__talk_read" in text
        assert "mcp__plugin_biff-dev_tty__read_messages" in text
        assert "mcp__plugin_biff-dev_tty__set_poll_interval" in text
