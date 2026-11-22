"""The plugin registry, and the service code that calls it through test doubles."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from biff.doctor import Diagnostics
from biff.enablement import RepoEnablement
from biff.plugins.contract import (
    CheckResult,
    Installer,
    PermissionChange,
    PluginRegistry,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class _Tool:
    """A tool that records every call and optionally refuses its grant."""

    name: str
    log: list[str]
    refuses: bool = False
    checks: tuple[CheckResult, ...] = field(default_factory=tuple)

    def preflight(self, root: Path) -> PermissionChange:
        self.log.append(f"{self.name}:preflight")
        return PermissionChange()

    def grant(self, root: Path) -> PermissionChange:
        self.log.append(f"{self.name}:grant")
        if self.refuses:
            return PermissionChange(error="refused", fix=f"Fix {self.name}")
        return PermissionChange(changed=(f"{self.name}-rule",), where=self.name)

    def revoke(
        self, root: Path, rules: Iterable[str] | None = None
    ) -> PermissionChange:
        self.log.append(f"{self.name}:revoke:{sorted(rules) if rules else rules}")
        return PermissionChange(changed=tuple(rules or ()), where=self.name)

    def register_guide(self, root: Path) -> bool:
        self.log.append(f"{self.name}:register")
        return True

    def unregister_guide(self, root: Path) -> bool:
        self.log.append(f"{self.name}:unregister")
        return True

    def check(self) -> tuple[CheckResult, ...]:
        return self.checks


def _registry(*tools: _Tool) -> PluginRegistry:
    registry = PluginRegistry()
    for tool in tools:
        installer: Installer = tool
        registry.register(tool.name, installer)
    return registry


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "init", "-q"],  # noqa: S607
        check=True,
        capture_output=True,
    )
    return root


class TestRegistry:
    def test_installers_in_registration_order(self) -> None:
        log: list[str] = []
        b, a = _Tool("b", log), _Tool("a", log)
        assert _registry(b, a).installers() == (b, a)

    def test_empty_registry_has_no_installers(self) -> None:
        assert PluginRegistry().installers() == ()

    def test_duplicate_name_refused(self) -> None:
        registry = PluginRegistry()
        registry.register("a", _Tool("a", []))
        with pytest.raises(ValueError, match="already registered: a"):
            registry.register("a", _Tool("a", []))

    def test_registries_are_independent(self) -> None:
        _registry(_Tool("a", []))
        assert PluginRegistry().installers() == ()


class TestEnableThroughRegistry:
    def test_every_registered_tool_is_granted_and_registered(
        self, tmp_path: Path
    ) -> None:
        log: list[str] = []
        repo = _repo(tmp_path / "repo")
        change = RepoEnablement(repo, _registry(_Tool("a", log), _Tool("b", log)))
        result = change.enable()
        assert [c.changed for c in result.permissions] == [("a-rule",), ("b-rule",)]
        assert log == [
            "a:preflight",
            "b:preflight",
            "a:grant",
            "b:grant",
            "a:register",
            "b:register",
        ]

    def test_refusal_revokes_earlier_grants_and_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        log: list[str] = []
        repo = _repo(tmp_path / "repo")
        tools = _registry(_Tool("a", log), _Tool("b", log, refuses=True))
        result = RepoEnablement(repo, tools).enable()
        assert result.failed
        assert "a:revoke:['a-rule']" in log
        assert not (repo / ".punt-labs" / "biff" / "enabled").exists()

    def test_refusal_report_names_the_tools_fix(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path / "repo")
        tools = _registry(_Tool("a", [], refuses=True))
        report = RepoEnablement(repo, tools).enable().enable_report()
        assert report[-1].endswith("Fix a and rerun biff enable.")

    def test_no_registered_tool_still_writes_the_marker(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path / "repo")
        RepoEnablement(repo, PluginRegistry()).enable()
        assert (repo / ".punt-labs" / "biff" / "enabled").is_file()


class TestDisableThroughRegistry:
    def test_every_registered_tool_is_revoked_and_unregistered(
        self, tmp_path: Path
    ) -> None:
        log: list[str] = []
        repo = _repo(tmp_path / "repo")
        RepoEnablement(repo, _registry(_Tool("a", log), _Tool("b", log))).disable()
        assert log == ["a:revoke:None", "b:revoke:None", "a:unregister", "b:unregister"]


class TestDoctorThroughRegistry:
    def test_tool_checks_are_printed_after_biffs(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        tool = _Tool(
            "a",
            [],
            checks=(CheckResult(name="Tool check", passed=True, message="fine"),),
        )
        Diagnostics(_registry(tool)).run()
        out = capsys.readouterr().out
        assert out.index("Tool check: fine") > out.index("CI workflow")

    def test_required_tool_failure_fails_the_run(self) -> None:
        tool = _Tool(
            "a", [], checks=(CheckResult(name="Tool", passed=False, message="bad"),)
        )
        assert Diagnostics(_registry(tool)).run() == 1
