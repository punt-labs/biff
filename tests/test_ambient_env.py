"""The suite's own isolation: ambient biff settings never reach a test."""

from __future__ import annotations

import os
import re
from pathlib import Path

from tests.conftest import _AMBIENT_BIFF_SETTINGS

_SRC = Path(__file__).resolve().parent.parent / "src" / "biff"


def test_ambient_biff_settings_are_stripped() -> None:
    leaked = [name for name in _AMBIENT_BIFF_SETTINGS if name in os.environ]
    assert leaked == []


def test_every_biff_setting_the_product_reads_is_stripped() -> None:
    read = {
        name
        for path in _SRC.rglob("*.py")
        for name in re.findall(r'"(BIFF_[A-Z_]+)"', path.read_text())
    }
    assert read <= set(_AMBIENT_BIFF_SETTINGS)
