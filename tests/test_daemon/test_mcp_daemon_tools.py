"""The read-only MCP tools, answered by biffd, say what the in-process tools say.

The golden conversation of ``tests/_mcp_golden.py`` runs here with ``who``,
``finger``, ``last`` and ``read_messages`` going through a real daemon; every
other tool still runs in the server's own process.  Both reach one local relay.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, final

import pytest
from fastmcp.client.transports import FastMCPTransport
from fastmcp.exceptions import ToolError

from biff._stdlib import get_repo_common_root
from biff.client.connection import (
    ClientIdentity,
    ConnectionLostError,
    DaemonClient,
    DaemonUnreachableError,
)
from biff.daemon.relays import ResolvedSession
from biff.daemon.server import BiffDaemon
from biff.daemon.session_store import CliSessionStore
from biff.markers import clear_plan_marker, read_plan_marker
from biff.models import BiffConfig
from biff.relay import LocalRelay
from biff.run_dir import RunDir
from biff.server.app import create_server
from biff.server.mcp_assembly import McpAssembly
from biff.server.mcp_link import McpLink
from biff.server.tools._descriptions import get_tty_name
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
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
    from pathlib import Path

    from fastmcp.tools import Tool
    from pydantic import JsonValue

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
        self.moved = False

    async def resolve(self, hello: Hello) -> ResolvedSession:
        config = BiffConfig(user=hello.user or "kai", repo_name=REPO)
        data_dir = self._base.parent / "moved" if self.moved else self._base
        return ResolvedSession(config=config, data_dir=data_dir, repo_root=None)


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
    resolver: _OneRepo
    asked: list[str] = field(default_factory=list[str])
    stack: AsyncExitStack = field(default_factory=AsyncExitStack)
    connected: list[DaemonClient] = field(default_factory=list[DaemonClient])

    def link(self, user: str, settle: float = 5.0) -> McpLink:
        async def connect(identity: ClientIdentity) -> DaemonClient:
            client = await DaemonClient.connect(
                run_dir=self.run_dir, port=self.daemon.port, identity=identity
            )
            self.connected.append(client)
            return client

        return McpLink(
            user=user, cwd="/work/repo", connect=connect, settle_seconds=settle
        )

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
        McpAssembly.answer_from(link or self.link(user), server, state)
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
    resolver = _OneRepo(tmp_path / "relay")
    daemon = BiffDaemon(
        run_dir=run_dir,
        port=0,
        sessions=CliSessionStore(tmp_path / "home" / "sessions"),
        resolver=resolver,
        relay_factory=relay_factory,
        heartbeat_interval=0.05,
        poll_interval=0.02,
        attach_timeout=0.5,
        request_timeout=2.0,
        read_budget=4000,
        org_refresh_interval=0.05,
    )
    await daemon.start()
    env = _Rig(
        base=tmp_path / "relay", daemon=daemon, run_dir=run_dir, resolver=resolver
    )
    original = DaemonClient.request_held

    async def counted(
        self: DaemonClient, method: str, **params: JsonValue
    ) -> HeldReply:
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
    McpAssembly.answer_from(rig.link("kai"), served, served_state)

    def listing(tools: Sequence[Tool]) -> dict[str, tuple[object, ...]]:
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


async def test_mail_in_a_reply_that_never_got_out_is_served_again(rig: _Rig) -> None:
    """The process died after the tool returned and before the client had the text."""
    writer = await rig.client("eric")
    link = rig.link("kai")
    reader = await rig.client("kai", link=link)
    await reader.call("tty", name="kaitty")
    await writer.call("write", to="kai:kaitty", message="hello")
    first = await reader.call("read_messages")

    await link.close()
    again = await (await rig.client("kai")).call("read_messages")

    assert ("hello" in first, "hello" in again) == (True, True)


async def test_mail_is_confirmed_once_the_process_has_outlived_the_reply(
    rig: _Rig,
) -> None:
    writer = await rig.client("eric")
    reader = await rig.client("kai", link=rig.link("kai", settle=0.05))
    await reader.call("tty", name="kaitty")
    await writer.call("write", to="kai:kaitty", message="hello")
    await reader.call("read_messages")

    await asyncio.sleep(0.4)
    again = await (await rig.client("kai")).call("read_messages")

    assert again == "No new messages."


async def test_a_malformed_finger_address_fails_as_it_does_in_process(
    rig: _Rig, tmp_path: Path
) -> None:
    served = await rig.client("kai")
    plain = _Caller("kai", await _plain(tmp_path, rig.stack))
    with pytest.raises(ToolError) as want:
        await plain.call("finger", user="kai:")

    with pytest.raises(ToolError) as got:
        await served.call("finger", user="kai:")

    assert str(got.value) == str(want.value)


async def test_reading_mail_counts_as_activity_and_an_empty_read_does_not(
    rig: _Rig,
) -> None:
    key = SessionRouting("kai-session").session_key("kai")
    relay = LocalRelay(rig.base)
    writer = await rig.client("eric")
    reader = await rig.client("kai")
    await reader.call("tty", name="kaitty")

    async def last_tool_at() -> datetime:
        row = await relay.get_session(key)
        assert row is not None
        return row.last_tool_at

    start = await last_tool_at()
    await asyncio.sleep(0.05)
    await reader.call("read_messages")
    after_empty = await last_tool_at()
    await writer.call("write", to="kai:kaitty", message="hello")
    await asyncio.sleep(0.05)
    await reader.call("read_messages")
    after_mail = await last_tool_at()

    assert (after_empty == start, after_mail > start) == (True, True)


async def test_moving_to_another_session_drops_the_old_connection(rig: _Rig) -> None:
    link = rig.link("kai")
    await link.ask("old-session", "who")

    await link.ask("new-session", "who")

    with pytest.raises(ConnectionLostError):
        await rig.connected[0].request("who")


async def test_a_session_biffd_holds_on_another_relay_says_how_to_let_it_go(
    rig: _Rig,
) -> None:
    link = rig.link("kai")
    first = await rig.client("kai", link=link)
    await first.call("who")
    await link.close()
    rig.resolver.moved = True
    resumed = await rig.client("kai")

    with pytest.raises(ToolError, match=r"biff daemon restart"):
        await resumed.call("who")


async def _plain(tmp_path: Path, stack: AsyncExitStack) -> RecordingClient:
    """An in-process server's client, over a relay of its own."""
    state = create_state(
        BiffConfig(user="kai", repo_name=REPO),
        tmp_path / "plain",
        hostname="test-host",
        pwd="/test",
    )
    stamped = StampedClient(FastMCPTransport(create_server(state)))
    await stack.enter_async_context(stamped)
    return RecordingClient(client=stamped, transcript=Transcript(title=""))


async def test_a_write_goes_to_biffd_and_an_empty_address_is_answered_in_text(
    rig: _Rig,
) -> None:
    kai = await rig.client("kai")

    sent = await kai.call("write", to="eric", message="hi")
    empty = await kai.call("write", to="", message="hi")

    assert (sent, empty, rig.asked.count("write")) == (
        "Message sent to eric.",
        "Empty user: give a user or user:tty to write to.",
        2,
    )


async def test_a_plan_goes_to_biffd_and_its_marker_is_written_here(rig: _Rig) -> None:
    kai = await rig.client("kai")
    root = get_repo_common_root()

    await kai.call("plan", message="mine")

    assert (rig.asked.count("plan"), read_plan_marker(root, "kai-session")) == (
        1,
        "mine",
    )


async def test_an_auto_plan_that_keeps_the_manual_one_rewrites_the_marker(
    rig: _Rig,
) -> None:
    kai = await rig.client("kai")
    root = get_repo_common_root()
    await kai.call("plan", message="mine")
    clear_plan_marker(root, "kai-session")

    text = await kai.call("plan", message="from a hook", source="auto")

    assert (text, read_plan_marker(root, "kai-session")) == (
        "Plan unchanged (manual):\n   mine",
        "mine",
    )


async def test_a_tty_name_is_claimed_by_biffd_and_kept_here_for_the_hints(
    rig: _Rig,
) -> None:
    kai = await rig.client("kai")

    text = await kai.call("tty", name="kaitty")

    assert (text, rig.asked.count("tty"), get_tty_name()) == (
        "TTY: kaitty",
        1,
        "kaitty",
    )


async def test_mesg_goes_to_biffd(rig: _Rig) -> None:
    kai = await rig.client("kai")

    off = await kai.call("mesg", enabled=False)
    on = await kai.call("mesg", enabled=True)

    assert (off, on, rig.asked.count("mesg")) == ("is n", "is y", 2)


async def test_a_relay_set_only_in_this_process_is_refused_by_name(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIFF_RELAY_URL", "nats://elsewhere:4222")
    kai = await rig.client("kai")

    with pytest.raises(ToolError, match="BIFF_RELAY_URL"):
        await kai.call("who")
