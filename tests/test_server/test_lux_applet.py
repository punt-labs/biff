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

from punt_lux.protocol import ProgressElement, SeparatorElement, TextElement

from biff.integration.lux import (
    _context_fraction,
    _cost_text,
    _git_text,
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
        with patch("biff.statusline.find_session_key", return_value="kai:tty1"):
            _tee_session_data(raw, session_data_dir=tmp_path)
        written = (tmp_path / "kai:tty1.json").read_text()
        assert written == raw

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        path = tmp_path / "kai:tty1.json"
        path.write_text("old")
        raw = '{"new": true}'
        with patch("biff.statusline.find_session_key", return_value="kai:tty1"):
            _tee_session_data(raw, session_data_dir=tmp_path)
        assert path.read_text() == raw

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        deep = tmp_path / "deep" / "nested"
        raw = '{"ok": true}'
        with patch("biff.statusline.find_session_key", return_value="kai:tty1"):
            _tee_session_data(raw, session_data_dir=deep)
        assert (deep / "kai:tty1.json").exists()

    def test_never_raises_on_write_error(self, tmp_path: Path) -> None:
        """OSError during write is silently swallowed."""
        blocker = tmp_path / "blocker"
        blocker.write_text("I am a file")
        bad_dir = blocker / "subdir"
        with patch("biff.statusline.find_session_key", return_value="kai:tty1"):
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

    def test_identity_shown(self) -> None:
        unread = SessionUnread("kai", 0, "tty1")
        elements = build_status_elements({}, unread)
        identity = next(
            e for e in elements if isinstance(e, TextElement) and e.id == "identity"
        )
        assert identity.content == "kai:tty1"

    def test_identity_no_tty(self) -> None:
        unread = SessionUnread("kai", 0, "")
        elements = build_status_elements({}, unread)
        identity = next(
            e for e in elements if isinstance(e, TextElement) and e.id == "identity"
        )
        assert identity.content == "kai"

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
        assert SeparatorElement in types

        ctx_el = next(e for e in elements if isinstance(e, ProgressElement))
        assert ctx_el.fraction == 0.42

        cost_el = next(
            e for e in elements if isinstance(e, TextElement) and e.id == "cost"
        )
        assert cost_el.content == "$1.50"

    def test_no_cost_omits_cost_element(self) -> None:
        session: dict[str, object] = {
            "context_window": {"used_percentage": 50},
        }
        unread = SessionUnread("kai", 0, "tty1")
        elements = build_status_elements(session, unread)
        cost_els = [
            e for e in elements if isinstance(e, TextElement) and e.id == "cost"
        ]
        assert len(cost_els) == 0

    def test_no_context_no_cost_no_separator(self) -> None:
        unread = SessionUnread("kai", 0, "tty1")
        elements = build_status_elements({}, unread)
        separators = [e for e in elements if isinstance(e, SeparatorElement)]
        assert len(separators) == 0

    def test_display_items_rendered(self) -> None:
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
        display_els = [
            e
            for e in elements
            if isinstance(e, TextElement) and e.id.startswith("display-")
        ]
        assert len(display_els) == 2
        assert "[wall]" in display_els[0].content
        assert "[talk]" in display_els[1].content

    def test_empty_display_items_skipped(self) -> None:
        unread = SessionUnread(
            "kai",
            0,
            "tty1",
            display_items=(DisplayItemView(kind="wall", text=""),),
        )
        elements = build_status_elements({}, unread)
        display_els = [
            e
            for e in elements
            if isinstance(e, TextElement) and e.id.startswith("display-")
        ]
        assert len(display_els) == 0

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
                DisplayItemView(kind="wall", text="msg1"),
                DisplayItemView(kind="talk", text="msg2"),
            ),
        )
        elements = build_status_elements(session, unread)
        ids = [e.id for e in elements if hasattr(e, "id") and e.id is not None]
        assert len(ids) == len(set(ids))
