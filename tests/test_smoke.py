"""Smoke tests to verify package imports and basic structure."""

from __future__ import annotations


def test_package_imports() -> None:
    """Verify the biff package is importable."""
    import biff

    assert biff.__name__ == "biff"
