"""Repo slug resolution under a git ``insteadOf`` gateway rewrite.

An agent container rewrites github remotes through the model gateway with
``url.<gateway>.insteadOf``.  ``git remote get-url origin`` applies that
rewrite; the raw ``remote.origin.url`` config value does not.  These tests
run real git with the rewrite in effect.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from biff._stdlib import get_repo_slug

if TYPE_CHECKING:
    from pathlib import Path

_GATEWAY = "http://192.168.20.102:8081/github/"


@pytest.fixture
def gateway_rewrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate git from ambient config and install the gateway rewrite."""
    config = tmp_path / "gitconfig"
    config.write_text(
        f'[url "{_GATEWAY}"]\n'
        "\tinsteadOf = https://github.com/\n"
        "\tinsteadOf = git@github.com:\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "0")


def _repo_with_origin(path: Path, url: str) -> Path:
    path.mkdir()
    for args in (("init", "-q"), ("remote", "add", "origin", url)):
        subprocess.run(  # noqa: S603
            ["git", "-C", str(path), *args],  # noqa: S607
            check=True,
            capture_output=True,
        )
    return path


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/punt-labs/punt-labs.git",
        "git@github.com:punt-labs/punt-labs.git",
    ],
)
def test_rewritten_remote_resolves_to_canonical_slug(
    tmp_path: Path, gateway_rewrite: None, origin: str
) -> None:
    repo = _repo_with_origin(tmp_path / "repo", origin)
    assert get_repo_slug(repo) == "punt-labs/punt-labs"
