"""Tests for the plugin contract and registry."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from biff.plugins.contract import (
    ArrivalKind,
    ArrivalSummary,
    DeliverySink,
    Installer,
    InstallOutcome,
    InstallScope,
    PluginRegistry,
    SessionStart,
    ToolAdapter,
    ToolCheck,
)


class _Installer:
    def install(self, scope: InstallScope, root: Path) -> InstallOutcome:
        return InstallOutcome(changed=(f"{scope}:{root.name}",))

    def uninstall(self, scope: InstallScope, root: Path) -> InstallOutcome:
        return InstallOutcome()

    def check(self, scope: InstallScope, root: Path) -> tuple[ToolCheck, ...]:
        return (ToolCheck(name="x", passed=True, message="ok"),)


class _Sink:
    def __init__(self) -> None:
        self.seen: list[ArrivalSummary] = []

    async def deliver(self, summary: ArrivalSummary) -> None:
        self.seen.append(summary)


class _Start:
    def context(self, payload: Mapping[str, object]) -> str:
        return f"hello {payload.get('who', '')}"


def _summary() -> ArrivalSummary:
    return ArrivalSummary(
        kind=ArrivalKind.MESSAGE, session_key="k", count=2, text="2 unread"
    )


class TestConformance:
    """A plain class with the right methods satisfies each Protocol."""

    def test_installer(self, tmp_path: Path) -> None:
        installer: Installer = _Installer()
        out = installer.install(InstallScope.REPO, tmp_path)
        assert out.changed == (f"repo:{tmp_path.name}",)

    async def test_sink(self) -> None:
        sink = _Sink()
        delivery: DeliverySink = sink
        await delivery.deliver(_summary())
        assert sink.seen == [_summary()]

    def test_session_start(self) -> None:
        start: SessionStart = _Start()
        assert start.context({"who": "kai"}) == "hello kai"


class TestRegistry:
    def test_get_returns_registered_adapter(self) -> None:
        reg = PluginRegistry()
        adapter = ToolAdapter(installer=_Installer())
        reg.register("claude-code", adapter)
        assert reg.get("claude-code") is adapter

    def test_names_in_registration_order(self) -> None:
        reg = PluginRegistry()
        reg.register("b", ToolAdapter())
        reg.register("a", ToolAdapter())
        assert reg.names() == ("b", "a")

    def test_duplicate_registration_refused(self) -> None:
        reg = PluginRegistry()
        reg.register("a", ToolAdapter())
        with pytest.raises(ValueError, match="already registered: a"):
            reg.register("a", ToolAdapter())

    def test_unknown_name_raises(self) -> None:
        with pytest.raises(KeyError, match="Unknown plugin: nope"):
            PluginRegistry().get("nope")

    def test_registries_are_independent(self) -> None:
        reg = PluginRegistry()
        reg.register("a", ToolAdapter())
        assert PluginRegistry().names() == ()
