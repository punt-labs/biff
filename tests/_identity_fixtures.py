"""Shared on-disk fixtures for identity tests."""

from __future__ import annotations

from pathlib import Path


def write_gitmodules_ethos(repo_root: Path) -> None:
    """Write ``.gitmodules`` declaring the ``.punt-labs/ethos`` submodule.

    Mirrors what ``git submodule add`` writes, without actually
    populating ``.punt-labs/ethos`` -- reproduces the exact on-disk shape
    of an uninitialized submodule (a plain ``git clone``, or
    ``git worktree add``, neither of which initializes submodules).
    """
    (repo_root / ".gitmodules").write_text(
        '[submodule ".punt-labs/ethos"]\n'
        "\tpath = .punt-labs/ethos\n"
        "\turl = git@github.com:punt-labs/team.git\n"
    )
