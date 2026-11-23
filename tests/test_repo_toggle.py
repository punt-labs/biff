"""The toggle the MCP ``biff`` tool runs returns the report ``biff enable`` prints."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from biff._cli_runtime import CliInvocation
from biff.enablement import RepoEnablement
from biff.git_hooks import HOOKS_DIR_UNRESOLVED_NOTICE
from biff.repo_toggle import EnablementToggle

if TYPE_CHECKING:
    from pathlib import Path


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "init", "-q"],  # noqa: S607
        check=True,
        capture_output=True,
    )
    return root


def test_enable_returns_the_enable_report(tmp_path: Path) -> None:
    plugins = CliInvocation.plugin_registry()
    expected = "\n".join(
        RepoEnablement(_repo(tmp_path / "a"), plugins).enable().enable_report()
    )
    assert EnablementToggle(plugins).enable(_repo(tmp_path / "b")) == expected


def test_enable_outside_a_repo_returns_the_notice(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    toggle = CliInvocation.repo_toggle()
    assert toggle.enable(plain) == HOOKS_DIR_UNRESOLVED_NOTICE


def test_disable_returns_the_disable_report(tmp_path: Path) -> None:
    plugins = CliInvocation.plugin_registry()
    enabled = [_repo(tmp_path / "a"), _repo(tmp_path / "b")]
    for repo in enabled:
        EnablementToggle(plugins).enable(repo)
    expected = "\n".join(RepoEnablement(enabled[0], plugins).disable().disable_report())
    assert EnablementToggle(plugins).disable(enabled[1]) == expected
