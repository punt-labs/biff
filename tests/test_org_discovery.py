"""One org-discovery path for the CLI and the MCP server (DES-034)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from biff.cli_session import HostHandle
from biff.config import ResolvedConfig
from biff.models import BiffConfig
from biff.nats_relay import NatsRelay
from biff.relay import LocalRelay


def _handle(relay: LocalRelay | NatsRelay, tmp_path: Path) -> HostHandle:
    config = BiffConfig(user="kai", repo_name="r", orgs=("a", "b"))
    resolved = ResolvedConfig(
        config=config, data_dir=tmp_path, repo_common_root=tmp_path
    )
    return HostHandle(host=None, resolved=resolved, relay=relay)  # type: ignore[arg-type]


async def test_nats_relay_unions_the_repos_of_every_org() -> None:
    relay = NatsRelay(url="nats://localhost:4222", repo_name="r")

    async def per_org(org: str) -> frozenset[str]:
        return frozenset({f"{org}__one", f"{org}__two"})

    relay.discover_repos_for_org = per_org  # type: ignore[method-assign]
    found = await relay.discover_repos_for_orgs(["a", "b"])
    assert found == {"a__one", "a__two", "b__one", "b__two"}


async def test_no_orgs_find_no_repos() -> None:
    relay = NatsRelay(url="nats://localhost:4222", repo_name="r")
    assert await relay.discover_repos_for_orgs([]) == frozenset()


async def test_cli_handle_asks_a_nats_relay(tmp_path: Path) -> None:
    relay = NatsRelay(url="nats://localhost:4222", repo_name="r")
    relay.discover_repos_for_orgs = AsyncMock(  # type: ignore[method-assign]
        return_value=frozenset({"a__x"})
    )
    assert await _handle(relay, tmp_path).org_repos() == {"a__x"}


async def test_cli_handle_on_the_local_relay_finds_no_org_repos(
    tmp_path: Path,
) -> None:
    assert await _handle(LocalRelay(tmp_path), tmp_path).org_repos() == frozenset()
