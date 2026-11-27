"""Fixtures shared by the supervision tests."""

from __future__ import annotations

import pytest

from tests.test_supervision.scripted import ScriptedSupervisor


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> ScriptedSupervisor:
    """Answer every supervisor command from rules, recording each one."""
    return ScriptedSupervisor().install(monkeypatch)
