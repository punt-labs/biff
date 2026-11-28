"""The suite's own isolation: ambient biff settings never reach a test."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from tests.conftest import _AMBIENT_BIFF_SETTINGS, strip_ambient_biff_settings

if TYPE_CHECKING:
    import pytest

_SRC = Path(__file__).resolve().parent.parent / "src" / "biff"

# Read on biff's behalf by punt_observe, so no literal appears under src/.
_READ_BY_DEPENDENCIES = frozenset({"BIFF_TELEMETRY"})


def test_ambient_biff_settings_are_stripped() -> None:
    leaked = [
        name
        for name in _AMBIENT_BIFF_SETTINGS
        if name in os.environ and name != "BIFF_MCP_TRANSPORT"
    ]
    assert leaked == []


def test_the_suite_pins_the_in_process_transport() -> None:
    # biffd is the product's default; a test that took it would start a real one.
    assert os.environ["BIFF_MCP_TRANSPORT"] == "inprocess"


def test_strip_removes_settings_that_were_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in _AMBIENT_BIFF_SETTINGS:
        monkeypatch.setenv(name, "ambient")
    monkeypatch.setenv("BIFF_TEST_NATS_URL", "kept")
    monkeypatch.setenv("BIFF_RELAY_IMAGE", "kept")

    strip_ambient_biff_settings(monkeypatch)

    assert [name for name in _AMBIENT_BIFF_SETTINGS if name in os.environ] == []
    assert os.environ["BIFF_TEST_NATS_URL"] == "kept"
    assert os.environ["BIFF_RELAY_IMAGE"] == "kept"


def test_every_biff_setting_the_product_reads_is_stripped() -> None:
    read: set[str] = set(_READ_BY_DEPENDENCIES)
    for path in _SRC.rglob("*.py"):
        text = path.read_text()
        literals = set(re.findall(r'"(BIFF_[A-Z_]+)"', text))
        symbols = set(re.findall(r"^(BIFF_[A-Z_]+)\b\s*[:=]", text, re.MULTILINE))
        read |= literals - symbols
    assert read <= set(_AMBIENT_BIFF_SETTINGS)
