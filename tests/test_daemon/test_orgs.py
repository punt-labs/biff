"""Org repos are rediscovered while a session lives, as the in-process server does."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from biff.daemon.orgs import OrgRepos
from biff.relay import LocalRelay

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from biff.models import UserSession
    from tests.test_daemon.conftest import Harness


class Discovering(LocalRelay):
    """A relay that can name the repos of an org, and can fail to."""

    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)
        self.repos: frozenset[str] = frozenset({"alpha"})
        self.calls = 0
        self.fail = False
        self.searched: list[frozenset[str]] = []

    async def get_sessions_for_repos(self, repos: frozenset[str]) -> list[UserSession]:
        self.searched.append(repos)
        return await super().get_sessions_for_repos(repos)

    async def discover_repos_for_orgs(self, orgs: Sequence[str]) -> frozenset[str]:
        self.calls += 1
        if self.fail:
            msg = "discovery down"
            raise OSError(msg)
        return self.repos


async def test_the_first_refresh_finds_the_orgs_repos(tmp_path: Path) -> None:
    relay = Discovering(tmp_path)
    orgs = OrgRepos(relay, ("acme",), interval=60.0)
    assert await orgs.refresh() is True
    assert orgs.current == {"alpha"}


async def test_a_refresh_inside_the_interval_does_not_ask_again(
    tmp_path: Path,
) -> None:
    relay = Discovering(tmp_path)
    orgs = OrgRepos(relay, ("acme",), interval=60.0)
    await orgs.refresh()
    relay.repos = frozenset({"alpha", "beta"})
    assert await orgs.refresh() is False
    assert (relay.calls, orgs.current) == (1, frozenset({"alpha"}))


async def test_a_refresh_after_the_interval_picks_up_a_new_repo(
    tmp_path: Path,
) -> None:
    relay = Discovering(tmp_path)
    orgs = OrgRepos(relay, ("acme",), interval=0.0)
    await orgs.refresh()
    relay.repos = frozenset({"alpha", "beta"})
    assert await orgs.refresh() is True
    assert orgs.current == {"alpha", "beta"}


async def test_an_empty_answer_keeps_the_repos_already_known(tmp_path: Path) -> None:
    relay = Discovering(tmp_path)
    orgs = OrgRepos(relay, ("acme",), interval=0.0)
    await orgs.refresh()
    relay.repos = frozenset()
    assert await orgs.refresh() is False
    assert orgs.current == {"alpha"}


async def test_a_failing_discovery_keeps_the_stale_repos_and_is_not_fatal(
    tmp_path: Path,
) -> None:
    relay = Discovering(tmp_path)
    orgs = OrgRepos(relay, ("acme",), interval=0.0)
    await orgs.refresh()
    relay.fail = True
    assert await orgs.refresh() is False
    assert orgs.current == {"alpha"}


async def test_a_relay_that_cannot_discover_is_never_asked(tmp_path: Path) -> None:
    orgs = OrgRepos(LocalRelay(tmp_path), ("acme",), interval=0.0)
    assert (await orgs.refresh(), orgs.current) == (False, frozenset())


async def test_no_orgs_means_no_discovery(tmp_path: Path) -> None:
    relay = Discovering(tmp_path)
    orgs = OrgRepos(relay, (), interval=0.0)
    assert (await orgs.refresh(), relay.calls) == (False, 0)


async def test_a_live_session_sees_a_repo_that_appears_after_it_registered(
    harness: Harness,
) -> None:
    relays: list[Discovering] = []

    def build(directory: Path) -> Discovering:
        relay = Discovering(directory)
        relays.append(relay)
        return relay

    harness.factory.relay_type = build
    await harness.connect("s-kai", "kai", cwd="/work/org-repo")
    (session,) = harness.daemon._core.sessions._sessions.values()
    assert session.context.org_repos == {"alpha"}
    relays[0].repos = frozenset({"alpha", "newcomer"})
    for _ in range(50):
        if "newcomer" in session.context.org_repos:
            break
        await asyncio.sleep(0.05)
    assert "newcomer" in session.context.visible_repos


async def test_a_write_after_rediscovery_searches_the_refreshed_scope(
    harness: Harness,
) -> None:
    relays: list[Discovering] = []

    def build(directory: Path) -> Discovering:
        relay = Discovering(directory)
        relays.append(relay)
        return relay

    harness.factory.relay_type = build
    kai = await harness.connect("s-kai", "kai", cwd="/work/org-repo")
    await kai.request("write", to="eric", message="before")
    assert "newcomer" not in relays[0].searched[-1]
    relays[0].repos = frozenset({"alpha", "newcomer"})
    for _ in range(50):
        await asyncio.sleep(0.05)
        await kai.request("write", to="eric", message="after")
        if "newcomer" in relays[0].searched[-1]:
            break
    assert "newcomer" in relays[0].searched[-1]
