"""``biff session new|ls|use|end`` against a real filesystem relay."""

from __future__ import annotations

from pathlib import Path

import pytest

from biff.cli_session_host import SessionHost
from biff.cli_session_pointer import CurrentPointer
from biff.cli_session_select import SessionSelectionError, SessionSelector
from biff.cli_session_store import CliSessionEndedError, CliSessionStore
from biff.commands.session import SessionVerbs
from biff.models import BiffConfig, Message
from biff.relay import LocalRelay
from biff.session_id import CLI_ROLE
from biff.session_routing import SessionRouting


@pytest.fixture()
def host(tmp_path: Path) -> SessionHost:
    root = tmp_path / "repo"
    root.mkdir()
    return SessionHost(
        relay=LocalRelay(tmp_path / "relay"),
        config=BiffConfig(user="kai", repo_name="biff"),
        selector=SessionSelector(
            store=CliSessionStore(tmp_path / "sessions"),
            pointer=CurrentPointer(tmp_path / "current"),
            user="kai",
            repo="biff",
            repo_root=root,
            hostname="h1",
        ),
        hostname="h1",
        pwd=str(root),
    )


class TestNew:
    async def test_prints_the_name(self, host: SessionHost) -> None:
        result = await SessionVerbs(host).new("main")
        assert not result.error
        assert result.text == "main"

    async def test_auto_name(self, host: SessionHost) -> None:
        assert (await SessionVerbs(host).new(None)).text == "tty1"

    async def test_taken_name_is_an_error_with_no_fallback(
        self, host: SessionHost
    ) -> None:
        await SessionVerbs(host).new("main")
        result = await SessionVerbs(host).new("main")
        assert result.error
        assert "already in use" in result.text
        assert len(host.selector.store.list()) == 1

    async def test_writes_the_pointer(self, host: SessionHost) -> None:
        await SessionVerbs(host).new("main")
        pointed = host.selector.pointed()
        assert pointed is not None
        assert pointed.tty == "main"


class TestLs:
    async def test_empty(self, host: SessionHost) -> None:
        result = await SessionVerbs(host).ls(include_ended=False)
        assert result.text == "No sessions."
        assert result.json_data == []

    async def test_lists_name_user_repo_state_and_id(self, host: SessionHost) -> None:
        record = await host.create("main")
        result = await SessionVerbs(host).ls(include_ended=False)
        assert "main" in result.text
        assert record.session_id in result.text
        assert result.json_data == [
            {
                "name": "main",
                "user": "kai",
                "repo": "biff",
                "state": "live",
                "last_attached": record.last_attached_at.isoformat(timespec="seconds"),
                "id": record.session_id,
            }
        ]

    async def test_ended_hidden_unless_all(self, host: SessionHost) -> None:
        await SessionVerbs(host).new("main")
        await SessionVerbs(host).end("main")
        hidden = await SessionVerbs(host).ls(include_ended=False)
        shown = await SessionVerbs(host).ls(include_ended=True)
        assert hidden.json_data == []
        assert "ended" in shown.text
        assert shown.json_data != []


class TestUse:
    async def test_writes_the_pointer_only(self, host: SessionHost) -> None:
        a = await host.create("alpha")
        await host.create("beta")  # beta is now the pointer
        result = await SessionVerbs(host).use("alpha")
        assert result.text == "alpha"
        assert host.selector.pointer.read(host.selector.repo_root) == a.session_id
        assert host.selector.store.read(a.session_id).state == "live"

    async def test_does_not_revive_a_dormant_session(self, host: SessionHost) -> None:
        a = await host.create("alpha")
        host.selector.store.lapse(a.session_id)
        await SessionVerbs(host).use("alpha")
        assert host.selector.store.read(a.session_id).state == "dormant"

    async def test_unknown_is_an_error(self, host: SessionHost) -> None:
        with pytest.raises(SessionSelectionError, match="unknown session: nope"):
            await SessionVerbs(host).use("nope")

    async def test_ended_is_the_named_error(self, host: SessionHost) -> None:
        await SessionVerbs(host).new("alpha")
        await SessionVerbs(host).end("alpha")
        with pytest.raises(CliSessionEndedError, match="session new --tty alpha"):
            await SessionVerbs(host).use("alpha")


class TestEnd:
    async def test_drains_unread_mail_then_ends(self, host: SessionHost) -> None:
        record = await host.create("alpha")
        key = SessionRouting(record.session_id, role=CLI_ROLE).session_key("kai")
        await host.relay.deliver(
            Message(from_user="eric", from_tty="tty2", to_user=key, body="hello")
        )

        result = await SessionVerbs(host).end("alpha")

        assert result.text == "ended alpha"
        assert host.selector.store.read(record.session_id).state == "ended"
        mailbox = await host.relay.fetch_user_inbox("kai")
        assert [m.body for m in mailbox] == ["hello"]

    async def test_by_id(self, host: SessionHost) -> None:
        record = await host.create("alpha")
        await SessionVerbs(host).end(record.session_id)
        assert host.selector.store.read(record.session_id).state == "ended"

    async def test_already_ended_is_an_error(self, host: SessionHost) -> None:
        await SessionVerbs(host).new("alpha")
        await SessionVerbs(host).end("alpha")
        with pytest.raises(CliSessionEndedError):
            await SessionVerbs(host).end("alpha")

    async def test_unknown_is_an_error(self, host: SessionHost) -> None:
        with pytest.raises(SessionSelectionError, match="unknown session"):
            await SessionVerbs(host).end("ghost")


class TestForeignRepo:
    async def test_end_refuses_a_session_of_another_repo(
        self, host: SessionHost, tmp_path: Path
    ) -> None:
        import dataclasses

        other_sel = dataclasses.replace(
            host.selector, repo="other", repo_root=tmp_path / "other"
        )
        other = dataclasses.replace(host, selector=other_sel)
        record = await host.create("alpha")
        key = SessionRouting(record.session_id, role=CLI_ROLE).session_key("kai")
        await host.relay.deliver(Message(from_user="eric", to_user=key, body="keep me"))

        with pytest.raises(SessionSelectionError, match="belongs to repo biff"):
            await SessionVerbs(other).end("alpha")

        assert host.selector.store.read(record.session_id).state == "live"
        assert await host.relay.get_session(key) is not None
        assert [m.body for m in await host.relay.fetch(key)] == ["keep me"]
