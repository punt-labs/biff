"""Tests for lux session status dashboard (biff-waaf).

Phase 1: _tee_session_data persists raw stdin JSON.
Phase 2: build_status_elements produces typed punt-lux elements.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("punt_lux", reason="punt-lux not installed")

from punt_lux.protocol import ProgressElement, TextElement, TreeElement

from biff.integration.lux import (
    _context_fraction,
    _cost_text,
    _git_text,
    _truncate,
    build_status_elements,
)
from biff.statusline import _tee_session_data
from biff.unread import DisplayItemView, SessionUnread

# --- Phase 1: _tee_session_data -------------------------------------------


class TestTeeSessionData:
    def test_writes_raw_json(self, tmp_path: Path) -> None:
        raw = json.dumps(
            {
                "workspace": {"project_dir": "/foo"},
                "cost": {"total_cost_usd": 1.5},
            }
        )
        with patch("biff.statusline.find_session_key", return_value=12345):
            _tee_session_data(raw, session_data_dir=tmp_path)
        written = (tmp_path / "12345.json").read_text()
        assert written == raw

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        path = tmp_path / "12345.json"
        path.write_text("old")
        raw = '{"new": true}'
        with patch("biff.statusline.find_session_key", return_value=12345):
            _tee_session_data(raw, session_data_dir=tmp_path)
        assert path.read_text() == raw

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        deep = tmp_path / "deep" / "nested"
        raw = '{"ok": true}'
        with patch("biff.statusline.find_session_key", return_value=12345):
            _tee_session_data(raw, session_data_dir=deep)
        assert (deep / "12345.json").exists()

    def test_never_raises_on_write_error(self, tmp_path: Path) -> None:
        """OSError during write is silently swallowed."""
        blocker = tmp_path / "blocker"
        blocker.write_text("I am a file")
        bad_dir = blocker / "subdir"
        with patch("biff.statusline.find_session_key", return_value=12345):
            _tee_session_data('{"data": 1}', session_data_dir=bad_dir)


# --- Phase 2: Element helpers ---------------------------------------------


class TestGitText:
    def test_workspace_object(self) -> None:
        session: dict[str, object] = {
            "workspace": {"project_dir": "/home/kai/biff"},
        }
        assert _git_text(session) == "biff"

    def test_workspace_string(self) -> None:
        assert _git_text({"workspace": "/home/kai/biff"}) == "biff"

    def test_workspace_missing(self) -> None:
        assert _git_text({}) == ""

    def test_workspace_non_dict_non_str(self) -> None:
        assert _git_text({"workspace": 42}) == ""

    def test_falls_back_to_current_dir(self) -> None:
        session: dict[str, object] = {
            "workspace": {"current_dir": "/x/repo"},
        }
        assert _git_text(session) == "repo"


class TestContextFraction:
    def test_used_percentage(self) -> None:
        session: dict[str, object] = {
            "context_window": {"used_percentage": 42},
        }
        assert _context_fraction(session) == 0.42

    def test_missing(self) -> None:
        assert _context_fraction({}) is None

    def test_non_dict(self) -> None:
        assert _context_fraction({"context_window": "bad"}) is None

    def test_zero(self) -> None:
        session: dict[str, object] = {
            "context_window": {"used_percentage": 0},
        }
        assert _context_fraction(session) == 0.0


class TestCostText:
    def test_positive(self) -> None:
        session: dict[str, object] = {
            "cost": {"total_cost_usd": 1.5},
        }
        assert _cost_text(session) == "$1.50"

    def test_zero(self) -> None:
        assert _cost_text({"cost": {"total_cost_usd": 0}}) == ""

    def test_missing(self) -> None:
        assert _cost_text({}) == ""


# --- Phase 2: build_status_elements --------------------------------------


class TestBuildStatusElements:
    def test_unconfigured_shows_not_configured(self) -> None:
        elements = build_status_elements({}, None)
        assert len(elements) == 1
        assert isinstance(elements[0], TextElement)
        assert "not configured" in elements[0].content

    def test_msg_status_shown(self) -> None:
        unread = SessionUnread("kai", 0, "tty1")
        elements = build_status_elements({}, unread)
        msg = next(
            e for e in elements if isinstance(e, TextElement) and e.id == "msg-status"
        )
        assert "0 messages" in msg.content

    def test_msg_status_no_tty(self) -> None:
        unread = SessionUnread("kai", 0, "")
        elements = build_status_elements({}, unread)
        msg = next(
            e for e in elements if isinstance(e, TextElement) and e.id == "msg-status"
        )
        assert "0 messages" in msg.content

    def test_message_count_plural(self) -> None:
        unread = SessionUnread("kai", 3, "tty1")
        elements = build_status_elements({}, unread)
        msg = next(
            e for e in elements if isinstance(e, TextElement) and e.id == "msg-status"
        )
        assert msg.content == "3 messages"

    def test_message_count_singular(self) -> None:
        unread = SessionUnread("kai", 1, "tty1")
        elements = build_status_elements({}, unread)
        msg = next(
            e for e in elements if isinstance(e, TextElement) and e.id == "msg-status"
        )
        assert msg.content == "1 message"

    def test_messaging_off(self) -> None:
        unread = SessionUnread("kai", 0, "tty1", biff_enabled=False)
        elements = build_status_elements({}, unread)
        msg = next(
            e for e in elements if isinstance(e, TextElement) and e.id == "msg-status"
        )
        assert msg.content == "messaging off"

    def test_full_session_with_cost(self) -> None:
        session: dict[str, object] = {
            "context_window": {"used_percentage": 42},
            "cost": {"total_cost_usd": 1.50},
        }
        unread = SessionUnread("kai", 2, "tty1")
        elements = build_status_elements(session, unread)

        types = [type(e) for e in elements]
        assert ProgressElement in types

        ctx_el = next(e for e in elements if isinstance(e, ProgressElement))
        assert ctx_el.fraction == 0.42

        # Cost is merged onto the msg-status line
        msg = next(
            e for e in elements if isinstance(e, TextElement) and e.id == "msg-status"
        )
        assert "$1.50" in msg.content
        assert "2 messages" in msg.content

    def test_no_cost_omits_cost_from_msg(self) -> None:
        session: dict[str, object] = {
            "context_window": {"used_percentage": 50},
        }
        unread = SessionUnread("kai", 0, "tty1")
        elements = build_status_elements(session, unread)
        msg = next(
            e for e in elements if isinstance(e, TextElement) and e.id == "msg-status"
        )
        assert "$" not in msg.content

    def test_no_context_no_progress_bar(self) -> None:
        unread = SessionUnread("kai", 0, "tty1")
        elements = build_status_elements({}, unread)
        progress = [e for e in elements if isinstance(e, ProgressElement)]
        assert len(progress) == 0

    def test_wall_tree_section(self) -> None:
        unread = SessionUnread(
            "kai",
            0,
            "tty1",
            display_items=(
                DisplayItemView(kind="wall", text="@admin: deploy freeze until 5pm"),
            ),
        )
        elements = build_status_elements({}, unread)
        wall = next(
            e for e in elements if isinstance(e, TreeElement) and e.id == "wall"
        )
        assert len(wall.nodes) == 1
        assert "deploy freeze" in wall.nodes[0]["label"]

    def test_talk_tree_section(self) -> None:
        unread = SessionUnread(
            "kai",
            0,
            "tty1",
            display_items=(DisplayItemView(kind="talk", text="@eric: check PR"),),
        )
        elements = build_status_elements({}, unread)
        talk = next(
            e for e in elements if isinstance(e, TreeElement) and e.id == "talk"
        )
        assert len(talk.nodes) == 1
        assert "check PR" in talk.nodes[0]["label"]

    def test_wall_and_talk_both_shown(self) -> None:
        unread = SessionUnread(
            "kai",
            0,
            "tty1",
            display_items=(
                DisplayItemView(kind="wall", text="@admin: freeze"),
                DisplayItemView(kind="talk", text="@eric: check PR"),
            ),
        )
        elements = build_status_elements({}, unread)
        trees = [e for e in elements if isinstance(e, TreeElement)]
        ids = {e.id for e in trees}
        assert "wall" in ids
        assert "talk" in ids

    def test_plan_tree_section(self) -> None:
        unread = SessionUnread(
            "kai",
            0,
            "tty1",
            plan="biff-waaf: lux widget UX redesign",
        )
        elements = build_status_elements({}, unread)
        plan_el = next(
            e for e in elements if isinstance(e, TreeElement) and e.id == "plan"
        )
        assert len(plan_el.nodes) == 1
        assert "biff-waaf" in plan_el.nodes[0]["label"]

    def test_no_plan_no_tree(self) -> None:
        unread = SessionUnread("kai", 0, "tty1")
        elements = build_status_elements({}, unread)
        plan_els = [
            e for e in elements if isinstance(e, TreeElement) and e.id == "plan"
        ]
        assert len(plan_els) == 0

    def test_empty_display_items_no_trees(self) -> None:
        unread = SessionUnread(
            "kai",
            0,
            "tty1",
            display_items=(DisplayItemView(kind="wall", text=""),),
        )
        elements = build_status_elements({}, unread)
        trees = [e for e in elements if isinstance(e, TreeElement)]
        assert len(trees) == 0

    def test_long_wall_truncated_in_node(self) -> None:
        long_msg = (
            "deploy freeze until 5pm — do not push to main under any circumstances"
        )
        unread = SessionUnread(
            "kai",
            0,
            "tty1",
            display_items=(DisplayItemView(kind="wall", text=f"@admin: {long_msg}"),),
        )
        elements = build_status_elements({}, unread)
        wall = next(
            e for e in elements if isinstance(e, TreeElement) and e.id == "wall"
        )
        node = wall.nodes[0]
        # Preview label is truncated
        assert len(node["label"]) <= 20
        assert node["label"].endswith("\u2026")
        # Full text in child node
        assert node["children"][0]["label"] == long_msg

    def test_short_wall_no_child_node(self) -> None:
        unread = SessionUnread(
            "kai",
            0,
            "tty1",
            display_items=(DisplayItemView(kind="wall", text="@admin: short"),),
        )
        elements = build_status_elements({}, unread)
        wall = next(
            e for e in elements if isinstance(e, TreeElement) and e.id == "wall"
        )
        node = wall.nodes[0]
        assert node["label"] == "short"
        assert "children" not in node

    def test_repo_shown(self) -> None:
        unread = SessionUnread("kai", 0, "tty1", repo="punt-labs__biff")
        elements = build_status_elements({}, unread)
        repo_el = next(
            e for e in elements if isinstance(e, TextElement) and e.id == "repo"
        )
        assert repo_el.content == "punt-labs/biff"

    def test_repo_hidden_when_empty(self) -> None:
        unread = SessionUnread("kai", 0, "tty1")
        elements = build_status_elements({}, unread)
        repo_els = [
            e for e in elements if isinstance(e, TextElement) and e.id == "repo"
        ]
        assert len(repo_els) == 0

    def test_all_ids_unique(self) -> None:
        session: dict[str, object] = {
            "context_window": {"used_percentage": 50},
            "cost": {"total_cost_usd": 2.0},
        }
        unread = SessionUnread(
            "kai",
            1,
            "tty1",
            display_items=(
                DisplayItemView(kind="wall", text="@admin: msg1"),
                DisplayItemView(kind="talk", text="@eric: msg2"),
            ),
            plan="working on stuff",
        )
        elements = build_status_elements(session, unread)
        ids = [e.id for e in elements if hasattr(e, "id") and e.id is not None]
        assert len(ids) == len(set(ids))


class TestTruncate:
    def test_short_text_unchanged(self) -> None:
        assert _truncate("hello") == "hello"

    def test_exact_length_unchanged(self) -> None:
        assert _truncate("a" * 20) == "a" * 20

    def test_long_text_truncated(self) -> None:
        result = _truncate("a" * 30)
        assert len(result) == 20
        assert result.endswith("\u2026")

    def test_custom_length(self) -> None:
        result = _truncate("hello world", length=8)
        assert len(result) == 8
        assert result.endswith("\u2026")
