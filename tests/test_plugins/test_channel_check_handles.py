"""Doctor checks the sessions an MCP server in this repo would register (DES-087).

Claude Code's biff server registers under the repo's agent identity, not under
the shell's human handle, so doctor looks up both when they differ.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from biff.nats_relay import NatsRelay
from biff.plugins.claude_code.channel_check_target import CheckHandles, CheckTarget

if TYPE_CHECKING:
    from pathlib import Path


def _agent_repo(root: Path, handle: str = "claude") -> Path:
    (root / ".punt-labs" / "ethos" / "identities").mkdir(parents=True)
    (root / ".punt-labs" / "ethos.yaml").write_text(f"agent: {handle}\n")
    (root / ".punt-labs" / "ethos" / "identities" / f"{handle}.yaml").write_text(
        f"handle: {handle}\nname: Claude Agento\nkind: agent\n"
    )
    return root


@pytest.fixture(autouse=True)
def _no_ambient_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BIFF_USER", raising=False)


def test_the_repos_agent_handle_comes_before_the_human_handle(tmp_path: Path) -> None:
    repo = _agent_repo(tmp_path)
    assert CheckHandles.resolve("kai", repo, None) == ("claude", "kai")


def test_one_handle_when_the_human_and_the_agent_are_the_same(tmp_path: Path) -> None:
    repo = _agent_repo(tmp_path, "kai")
    assert CheckHandles.resolve("kai", repo, None) == ("kai",)


def test_an_override_is_the_handle_the_server_would_use(tmp_path: Path) -> None:
    repo = _agent_repo(tmp_path)
    assert CheckHandles.resolve("zed", repo, "zed") == ("zed",)


def test_biff_user_in_the_environment_is_honored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIFF_USER", "env-user")
    repo = _agent_repo(tmp_path)
    assert CheckHandles.resolve("kai", repo, None) == ("env-user", "kai")


def test_the_cli_user_override_reaches_the_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``biff --user zed doctor`` checks zed, not the shell's own handle."""
    monkeypatch.setenv("BIFF_RELAY_URL", "nats://127.0.0.1:1")  # never dialed
    target = CheckTarget.from_environment("zed")
    assert target.users == ("zed",)
    assert isinstance(target.relay, NatsRelay)
