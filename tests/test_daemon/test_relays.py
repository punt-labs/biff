"""Relay targets decide which sessions share a relay connection."""

from __future__ import annotations

from pathlib import Path

import pytest

from biff.daemon.relays import RelayPool, ResolvedSession
from biff.models import BiffConfig, RelayAuth
from biff.nats_relay import NatsRelay
from biff.relay import LocalRelay, Relay


def _session(
    tmp_path: Path, *, url: str | None = None, token: str | None = None, name: str = "a"
) -> ResolvedSession:
    return ResolvedSession(
        config=BiffConfig(
            user="kai",
            repo_name="test",
            relay_url=url,
            relay_auth=RelayAuth(token=token) if token else None,
        ),
        data_dir=tmp_path / name,
        repo_root=None,
    )


def test_local_sessions_share_a_target_per_data_directory(tmp_path: Path) -> None:
    assert _session(tmp_path).target == _session(tmp_path).target
    assert _session(tmp_path, name="a").target != _session(tmp_path, name="b").target


def test_nats_sessions_of_one_repo_share_a_target_whatever_their_data_directory(
    tmp_path: Path,
) -> None:
    one = _session(tmp_path, url="tls://relay.example", token="t", name="a")
    two = _session(tmp_path, url="tls://relay.example", token="t", name="b")
    assert one.target == two.target


def test_nats_credentials_are_part_of_the_target(tmp_path: Path) -> None:
    one = _session(tmp_path, url="tls://relay.example", token="one")
    two = _session(tmp_path, url="tls://relay.example", token="two")
    assert one.target != two.target


def test_pool_builds_once_per_target(tmp_path: Path) -> None:
    built: list[ResolvedSession] = []

    def factory(resolved: ResolvedSession) -> Relay:
        built.append(resolved)
        return LocalRelay(resolved.data_dir)

    pool = RelayPool(factory)
    first = pool.acquire(_session(tmp_path))
    second = pool.acquire(_session(tmp_path))
    pool.acquire(_session(tmp_path, name="b"))
    assert (first is second, len(built), pool.connection_count) == (True, 2, 2)


def test_the_selecting_factory_builds_a_local_relay_without_a_url(
    tmp_path: Path,
) -> None:
    relay = RelayPool.selecting()(_session(tmp_path))
    assert isinstance(relay, LocalRelay)


def _repo_session(
    tmp_path: Path, repo: str, url: str = "tls://relay.example"
) -> ResolvedSession:
    return ResolvedSession(
        config=BiffConfig(user="kai", repo_name=repo, relay_url=url),
        data_dir=tmp_path / "shared",
        repo_root=None,
    )


def test_two_repos_on_one_nats_server_are_two_targets(tmp_path: Path) -> None:
    a = _repo_session(tmp_path, "repo-a")
    b = _repo_session(tmp_path, "repo-b")
    assert a.target != b.target


def test_two_repos_on_one_nats_server_get_two_relays_scoped_to_their_repo(
    tmp_path: Path,
) -> None:
    pool = RelayPool(RelayPool.selecting())
    a = pool.acquire(_repo_session(tmp_path, "repo-a"))
    b = pool.acquire(_repo_session(tmp_path, "repo-b"))
    assert (a is not b, pool.connection_count) == (True, 2)
    assert isinstance(a, NatsRelay)
    assert isinstance(b, NatsRelay)
    assert (a._repo_name, b._repo_name) == ("repo-a", "repo-b")


class _ClosingRelay(LocalRelay):
    """A local relay whose close can fail in a chosen way."""

    def __init__(self, data_dir: Path, failure: BaseException | None) -> None:
        super().__init__(data_dir)
        self._failure = failure
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        if self._failure is not None:
            raise self._failure


def _pool_of(tmp_path: Path, failures: list[BaseException | None]) -> RelayPool:
    made = iter(failures)

    def factory(resolved: ResolvedSession) -> Relay:
        return _ClosingRelay(resolved.data_dir, next(made))

    pool = RelayPool(factory)
    for index in range(len(failures)):
        pool.acquire(_session(tmp_path, name=f"relay-{index}"))
    return pool


async def test_a_relay_error_on_close_is_logged_and_the_rest_still_close(
    tmp_path: Path,
) -> None:
    pool = _pool_of(tmp_path, [OSError("link down"), None])
    await pool.close()
    assert pool.connection_count == 0


async def test_a_bug_on_close_surfaces_after_every_relay_was_closed(
    tmp_path: Path,
) -> None:
    pool = _pool_of(tmp_path, [RuntimeError("bug"), None])
    with pytest.raises(ExceptionGroup) as caught:
        await pool.close()
    assert [type(e) for e in caught.value.exceptions] == [RuntimeError]
    assert pool.connection_count == 0
