"""The pre-commit test selector and the quarantine policy."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from tools.test_changed import select_tests
from tools.test_quarantine import quarantine_is_valid

_ROOT = Path(__file__).resolve().parent.parent
_TODAY = date(2026, 11, 23)


def _mark(*args: object, **kwargs: object) -> pytest.Mark:
    return pytest.mark.quarantine(*args, **kwargs).mark


class TestQuarantine:
    def test_tracked_owned_short_quarantine_is_valid(self) -> None:
        mark = _mark(issue="PL-1", owner="Jim", expires="2026-12-07")
        assert quarantine_is_valid(mark, _TODAY)

    def test_expiry_today_is_valid(self) -> None:
        mark = _mark(issue="PL-1", owner="Jim", expires="2026-11-23")
        assert quarantine_is_valid(mark, _TODAY)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"owner": "Jim", "expires": "2026-12-07"},
            {"issue": "biff-7xd", "owner": "Jim", "expires": "2026-12-07"},
            {"issue": "PL-1", "owner": " ", "expires": "2026-12-07"},
            {"issue": "PL-1", "owner": "Jim"},
            {"issue": "PL-1", "owner": "Jim", "expires": "2026-11-22"},
            {"issue": "PL-1", "owner": "Jim", "expires": "2026-12-08"},
            {"issue": "PL-1", "owner": "Jim", "expires": "2026-02-30"},
            {"issue": "PL-1", "owner": "Jim", "expires": "12/07/2026"},
        ],
    )
    def test_untracked_unowned_or_long_quarantine_is_invalid(
        self, kwargs: dict[str, object]
    ) -> None:
        assert not quarantine_is_valid(_mark(**kwargs), _TODAY)

    def test_positional_arguments_are_invalid(self) -> None:
        mark = _mark("flaky", issue="PL-1", owner="Jim", expires="2026-12-07")
        assert not quarantine_is_valid(mark, _TODAY)


class TestSelectTests:
    def test_nothing_staged_runs_nothing(self) -> None:
        assert select_tests(_ROOT, set()) == []

    def test_a_changed_test_runs_itself(self) -> None:
        path = Path("tests/test_tty.py")
        assert select_tests(_ROOT, {path}) == [path]

    def test_a_changed_module_runs_its_importers(self) -> None:
        selected = select_tests(_ROOT, {Path("src/biff/tty.py")})
        assert selected is not None
        assert Path("tests/test_tty.py") in selected

    @pytest.mark.parametrize(
        "path",
        [
            "pyproject.toml",
            "tests/conftest.py",
            "tests/test_server/conftest.py",
            "tools/check_python_docs.py",
            ".github/workflows/test.yml",
            "src/biff/__init__.py",
            "src/biff/data/demo.creds",
            "scripts/smoke-release.sh",
        ],
    )
    def test_shared_or_unmapped_paths_run_the_whole_suite(self, path: str) -> None:
        assert select_tests(_ROOT, {Path(path)}) is None

    def test_docs_only_change_runs_nothing(self) -> None:
        assert select_tests(_ROOT, {Path("README.md")}) == []
