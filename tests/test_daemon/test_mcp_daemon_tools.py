"""The read-only MCP tools, answered by biffd, say what the in-process tools say.

The golden conversation of ``tests/_mcp_golden.py`` runs here with ``who``,
``finger``, ``last`` and ``read_messages`` going through a real daemon; every
other tool still runs in the server's own process.  Both reach one local relay.
"""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, final

import pytest
from fastmcp.client.transports import FastMCPTransport
from fastmcp.exceptions import ToolError

from biff.client.connection import ClientIdentity, DaemonClient, DaemonUnreachableError
from biff.daemon.relays import ResolvedSession
from biff.daemon.server import BiffDaemon
from biff.daemon.session_store import CliSessionStore
from biff.models import BiffConfig
from biff.relay import LocalRelay
from biff.run_dir import RunDir
from biff.server.app import create_server
from biff.server.mcp_assembly import McpAssembly
from biff.server.mcp_link import McpLink
from biff.session_routing import SessionRouting
from biff.testing import RecordingClient, Transcript
from biff.testing.caller import StampedClient, caller_for
from tests._mcp_golden import (
    STEPS,
    HistoryRelay,
    differences,
    isolate_machine,
    last_table,
)
from tests._state import create_state

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable
    from pathlib import Path

    from biff.client.held_reply import HeldReply
    from biff.frame_client import Hello
    from biff.relay import Relay

REPO = "_test-integration"
READS = frozenset({"who", "finger", "last", "read"})


@final
class _OneRepo:
    """Seats every hello in the one repository, on the relay under *base*."""

    def __init__(self, base: Path) -> None:
        self._base = base

    async def resolve(self, hello: Hello) -> ResolvedSession:
        config = BiffConfig(user=hello.user or "kai", repo_name=REPO)
        return ResolvedSession(config=config, data_dir=self._base, repo_root=None)


def _local(resolved: ResolvedSession) -> Relay:
    return LocalRelay(resolved.data_dir)


@dataclass(frozen=True)
class _Caller:
    """One user's MCP client, whose every call names that user's own session."""

    user: str
    recording: RecordingClient

    async def call(self, tool: str, **args: object) -> str:
        """Call *tool* as this user's session and return its text."""
        caller = caller_for(f"{self.user}-session")
        return await self.recording.call(tool, _caller=caller, **args)


@dataclass
class _Rig:
    """A daemon, and the MCP servers of two users that reach it."""

    base: Path
    daemon: BiffDaemon
    run_dir: RunDir
    asked: list[str] = field(default_factory=list[str])
    stack: AsyncExitStack = field(default_factory=AsyncExitStack)
    connected: list[DaemonClient] = field(default_factory=list[DaemonClient])

    def link(self, user: str) -> McpLink:
        async def connect(identity: ClientIdentity) -> DaemonClient:
            client = await DaemonClient.connect(
                run_dir=self.run_dir, port=self.daemon.port, identity=identity
            )
            self.connected.append(client)
            return client

        return McpLink(user=user, cwd="/work/repo", connect=connect)

    async def client(
        self, user: str, link: McpLink | None = None, relay: Relay | None = None
    ) -> _Caller:
        """Return *user*'s MCP client, over a server that asks biffd for reads."""
        state = create_state(
            BiffConfig(user=user, repo_name=REPO),
            self.base,
            relay=relay,
            hostname="test-host",
            pwd="/test",
        )
        server = create_server(state)
        McpAssembly.answer_reads_from(link or self.link(user), server, state)
        stamped = StampedClient(FastMCPTransport(server))
        await self.stack.enter_async_context(stamped)
        recording = RecordingClient(
            client=stamped, transcript=Transcript(title=""), user=user
        )
        return _Caller(user, recording)


@asynccontextmanager
async def _running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relay_factory: Callable[[ResolvedSession], Relay],
) -> AsyncGenerator[_Rig]:
    """A daemon over the relays *relay_factory* builds, counting what clients ask."""
    run_dir = RunDir(tmp_path / "run")
    daemon = BiffDaemon(
        run_dir=run_dir,
        port=0,
        sessions=CliSessionStore(tmp_path / "home" / "sessions"),
        resolver=_OneRepo(tmp_path / "relay"),
        relay_factory=relay_factory,
        heartbeat_interval=0.05,
        poll_interval=0.02,
        attach_timeout=0.5,
        request_timeout=2.0,
        read_budget=4000,
        org_refresh_interval=0.05,
    )
    await daemon.start()
    env = _Rig(base=tmp_path / "relay", daemon=daemon, run_dir=run_dir)
    original = DaemonClient.request_held

    async def counted(self: DaemonClient, method: str, **params: Any) -> HeldReply:  # pyright: ignore[reportExplicitAny]
        env.asked.append(method)
        return await original(self, method, **params)

    monkeypatch.setattr(DaemonClient, "request_held", counted)
    try:
        yield env
    finally:
        await env.stack.aclose()
        await daemon.stop()


@pytest.fixture(autouse=True)
def _private_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolate_machine(monkeypatch, tmp_path / "machine")


@pytest.fixture
async def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Rig]:
    async with _running(tmp_path, monkeypatch, _local) as env:
        yield env


async def test_every_tool_returns_its_golden_text_with_reads_served_by_biffd(
    rig: _Rig,
) -> None:
    clients = {user: await rig.client(user) for user in ("kai", "eric")}

    actual = [await clients[s.user].call(s.tool, **s.args) for s in STEPS]

    assert differences(STEPS, actual) == []


async def test_the_reads_went_to_biffd(rig: _Rig) -> None:
    kai = await rig.client("kai")
    for tool, args in (
        ("who", {}),
        ("finger", {"user": "kai"}),
        ("last", {}),
        ("read_messages", {}),
    ):
        await kai.call(tool, **args)

    assert set(rig.asked) == READS


async def test_the_tools_a_daemon_server_lists_are_the_ones_the_in_process_server_lists(
    rig: _Rig, tmp_path: Path
) -> None:
    config = BiffConfig(user="kai", repo_name=REPO)
    plain = create_server(create_state(config, tmp_path / "plain"))
    served_state = create_state(config, tmp_path / "served")
    served = create_server(served_state)
    McpAssembly.answer_reads_from(rig.link("kai"), served, served_state)

    def listing(tools: list[Any]) -> dict[str, tuple[object, ...]]:  # pyright: ignore[reportExplicitAny]
        return {t.name: (t.description, t.parameters, t.meta) for t in tools}

    assert listing(list(await served.list_tools())) == listing(
        list(await plain.list_tools())
    )


async def test_mail_stays_unread_until_the_tool_has_returned_it(rig: _Rig) -> None:
    sender = await rig.client("eric")
    reader = await rig.client("kai")
    await reader.call("tty", name="kaitty")
    await sender.call("write", to="kai:kaitty", message="hello")

    first = await reader.call("read_messages")
    second = await reader.call("read_messages")

    assert ("hello" in first, second) == (True, "No new messages.")


async def test_a_confirmation_biffd_never_gets_leaves_the_mail_for_the_next_read(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    sender = await rig.client("eric")
    reader = await rig.client("kai")
    await reader.call("tty", name="kaitty")
    await sender.call("write", to="kai:kaitty", message="hello")

    async def lost(_self: object) -> None:
        msg = "the connection dropped before the confirmation"
        raise ConnectionError(msg)

    with monkeypatch.context() as patch:
        from biff.client.held_reply import HeldReply

        patch.setattr(HeldReply, "confirm", lost)
        first = await reader.call("read_messages")
    second = await reader.call("read_messages")

    assert ("hello" in first, "hello" in second) == (True, True)


async def test_a_lost_connection_is_opened_again_and_the_read_repeated(
    rig: _Rig,
) -> None:
    kai = await rig.client("kai")
    await kai.call("who")
    for client in rig.connected:
        await client.close()

    assert "kai:" in await kai.call("who")


async def test_with_no_daemon_a_read_is_a_tool_error_that_names_the_cause(
    rig: _Rig,
) -> None:
    async def refused(_identity: ClientIdentity) -> DaemonClient:
        msg = "biffd is not installed. Run: biff install"
        raise DaemonUnreachableError(msg)

    kai = await rig.client(
        "kai", McpLink(user="kai", cwd="/work/repo", connect=refused)
    )

    with pytest.raises(ToolError, match=r"biffd is not installed\. Run: biff install"):
        await kai.call("who")


@pytest.mark.parametrize(
    ("now", "login", "rank"),
    [
        pytest.param(
            datetime(2026, 12, 1, 9, 15, tzinfo=UTC), "Tue Dec 01 09:15", 0, id="after"
        ),
        pytest.param(
            datetime(2026, 1, 5, 7, 0, tzinfo=UTC), "Mon Jan 05 07:00", 2, id="before"
        ),
    ],
)
async def test_last_prints_the_table_the_in_process_server_prints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
    login: str,
    rank: int,
) -> None:
    """Session history through biffd, over a relay that keeps it, under two clocks."""
    key = SessionRouting("kai-session").session_key("kai")
    history = HistoryRelay(tmp_path / "relay", own_key=key, clock=now)
    async with _running(tmp_path, monkeypatch, lambda _resolved: history) as rig:
        kai = await rig.client("kai", relay=history)
        await kai.call("who")
        await history.seed_other_sessions()

        text = await kai.call("last")

    assert text == last_table(login, rank)
