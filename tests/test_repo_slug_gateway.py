"""Repo slug resolution under a git ``insteadOf`` gateway rewrite.

An agent container rewrites github remotes through the model gateway with
``url.<gateway>.insteadOf``.  ``git remote get-url origin`` applies that
rewrite; the raw ``remote.origin.url`` config value does not.  These tests
run real git with the rewrite in effect.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from biff import _stdlib
from biff._stdlib import get_repo_slug

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


def _git_stdout(path: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(path), *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_stdout_lines(path: Path, *args: str) -> list[str]:
    """Return git's output lines; a failing command fails the test, not passes it."""
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(path), *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()


def _git(path: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603
        ["git", "-C", str(path), *args],  # noqa: S607
        check=True,
        capture_output=True,
    )


def _repo_with_origin(path: Path, url: str) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "remote", "add", "origin", url)
    return path


def _add_rewrite(config: Path, base: str, prefix: str) -> None:
    with config.open("a") as f:
        f.write(f'[url "{base}"]\n\tinsteadOf = {prefix}\n')


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


def test_first_origin_url_wins_over_a_later_mirror(
    tmp_path: Path, gateway_rewrite: None
) -> None:
    repo = _repo_with_origin(tmp_path / "repo", "https://github.com/team/project.git")
    _git(
        repo, "remote", "set-url", "--add", "origin", "https://github.com/x/mirror.git"
    )
    assert get_repo_slug(repo) == "team/project"


def test_shorthand_alias_resolves_through_its_expansion(
    tmp_path: Path, gateway_rewrite: None
) -> None:
    _add_rewrite(tmp_path / "gitconfig", "https://github.com/", "gh:")
    repo = _repo_with_origin(tmp_path / "repo", "gh:team/project.git")
    assert get_repo_slug(repo) == "team/project"


def test_raw_value_that_parses_wins_over_a_different_expansion(
    tmp_path: Path, gateway_rewrite: None
) -> None:
    _add_rewrite(
        tmp_path / "gitconfig", "https://github.com/other/", "https://example.org/team/"
    )
    repo = _repo_with_origin(tmp_path / "repo", "https://example.org/team/project.git")
    # The expansion is the valid slug other/project; the raw one must still win.
    assert _git_stdout(repo, "remote", "get-url", "origin").endswith(
        "other/project.git"
    )
    assert get_repo_slug(repo) == "team/project"


_EX = "https://example.org/team/"


@pytest.mark.parametrize(
    "entries",
    [
        pytest.param(["first", "", "b"], id="empty-in-the-middle"),
        pytest.param(["", "first"], id="empty-first"),
        pytest.param(["first", "", "b", "c"], id="reset-then-two-urls"),
        pytest.param(["first", ""], id="empty-last"),
    ],
)
def test_empty_url_entry_agrees_with_git(
    tmp_path: Path, gateway_rewrite: None, entries: list[str]
) -> None:
    # Git decides what an empty remote.origin.url entry means: Git 2.43 keeps the
    # earlier entries, Git 2.56 treats it as a reset.  The oracle is Git's own
    # effective list (`remote get-url --all`): its first non-empty entry.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for name in entries:
        _git(
            repo,
            "config",
            "--add",
            "remote.origin.url",
            f"{_EX}{name}.git" if name else "",
        )
    effective = _git_stdout_lines(repo, "remote", "get-url", "--all", "origin")
    fetch_url = next((u for u in effective if u), "")
    expected = _stdlib._parse_repo_slug(fetch_url)  # pyright: ignore[reportPrivateUsage]
    assert get_repo_slug(repo) == expected


@pytest.mark.parametrize(
    ("effective", "slug"),
    [
        pytest.param(["a", "", "b"], "team/a", id="git-2.43-keeps-earlier-entries"),
        pytest.param(["b"], "team/b", id="git-2.56-reset-drops-earlier-entries"),
    ],
)
def test_effective_list_decides_what_an_empty_entry_means(
    monkeypatch: pytest.MonkeyPatch, effective: list[str], slug: str
) -> None:
    # Both Git behaviors, simulated, so the 2.56 branch is covered on a 2.43 host.
    def fake(_root: Path, *args: str) -> list[str]:
        if args[:2] == ("config", "--get-all"):
            return [f"{_EX}a.git", "", f"{_EX}b.git"]
        assert args == ("remote", "get-url", "--all", "origin")
        return [f"{_EX}{n}.git" if n else "" for n in effective]

    monkeypatch.setattr(_stdlib, "_git_lines", fake)
    assert get_repo_slug(Path()) == slug


def test_expansion_is_not_computed_when_the_raw_url_parses(
    tmp_path: Path, gateway_rewrite: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_with_origin(tmp_path / "repo", "https://github.com/team/project.git")
    calls: list[tuple[str, ...]] = []

    def counting(root: Path, *args: str) -> list[str]:
        calls.append(args)
        return real(root, *args)

    real = _stdlib._git_lines  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(_stdlib, "_git_lines", counting)
    assert get_repo_slug(repo) == "team/project"
    assert len(calls) == 1
