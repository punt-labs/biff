"""Tests for bead ID auto-expansion in /plan (biff-5zq)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from biff._stdlib import expand_bead_id


class TestExpandBeadId:
    """Bead ID detection and title resolution."""

    def test_expands_valid_bead_id(self) -> None:
        title = "post-checkout hook: update plan from branch"
        bd_output = json.dumps([{"title": title}])
        with patch("biff._stdlib.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = bd_output
            result = expand_bead_id("biff-ka4")
        assert result == f"biff-ka4: {title}"

    def test_passes_through_non_bead_text(self) -> None:
        assert expand_bead_id("reviewing PR #64") == "reviewing PR #64"

    def test_passes_through_sentence(self) -> None:
        msg = "working on the hook dispatcher"
        assert expand_bead_id(msg) == msg

    def test_passes_through_already_expanded(self) -> None:
        msg = "biff-ka4: post-checkout hook"
        assert expand_bead_id(msg) == msg

    def test_passes_through_uppercase(self) -> None:
        assert expand_bead_id("BIFF-KA4") == "BIFF-KA4"

    def test_matches_short_id(self) -> None:
        bd_output = json.dumps([{"title": "Fix bug"}])
        with patch("biff._stdlib.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = bd_output
            result = expand_bead_id("biff-x1")
        assert result == "biff-x1: Fix bug"

    def test_matches_4char_suffix(self) -> None:
        bd_output = json.dumps([{"title": "Epic task"}])
        with patch("biff._stdlib.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = bd_output
            result = expand_bead_id("biff-mx3a")
        assert result == "biff-mx3a: Epic task"

    def test_rejects_5char_suffix(self) -> None:
        assert expand_bead_id("biff-abcde") == "biff-abcde"

    def test_rejects_1char_suffix(self) -> None:
        assert expand_bead_id("biff-a") == "biff-a"

    def test_different_prefix(self) -> None:
        bd_output = json.dumps([{"title": "Quarry feature"}])
        with patch("biff._stdlib.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = bd_output
            result = expand_bead_id("quarry-r9f")
        assert result == "quarry-r9f: Quarry feature"

    def test_fallback_when_bd_not_installed(self) -> None:
        with patch(
            "biff._stdlib.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            assert expand_bead_id("biff-ka4") == "biff-ka4"

    def test_fallback_when_bd_fails(self) -> None:
        with patch("biff._stdlib.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            assert expand_bead_id("biff-ka4") == "biff-ka4"

    def test_fallback_when_bd_returns_empty(self) -> None:
        with patch("biff._stdlib.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "[]"
            assert expand_bead_id("biff-ka4") == "biff-ka4"

    def test_fallback_on_invalid_json(self) -> None:
        with patch("biff._stdlib.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "not json"
            assert expand_bead_id("biff-ka4") == "biff-ka4"

    def test_fallback_on_timeout(self) -> None:
        with patch(
            "biff._stdlib.subprocess.run",
            side_effect=TimeoutError,
        ):
            assert expand_bead_id("biff-ka4") == "biff-ka4"

    def test_fallback_on_empty_title(self) -> None:
        bd_output = json.dumps([{"title": ""}])
        with patch("biff._stdlib.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = bd_output
            assert expand_bead_id("biff-ka4") == "biff-ka4"

    @pytest.mark.parametrize(
        "message",
        [
            "ls -la",
            "git checkout main",
            "biff-",
            "-ka4",
            "biff_ka4",
            "BIFF-KA4",
            "biff-KA4",
            "biff-ka4 extra text",
            "prefix biff-ka4",
        ],
    )
    def test_non_matching_patterns(self, message: str) -> None:
        assert expand_bead_id(message) == message
