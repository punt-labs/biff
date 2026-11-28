"""SessionHost: create, attach, five-rung resolution, end-drains-inbox, sweep."""

from __future__ import annotations

import asyncio
import dataclasses
import threading
import time
import uuid
from collections.abc import Callable, Coroutine, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from biff.daemon.session_drain import InboxDrain
from biff.daemon.session_host import LEASE, RETENTION, SessionHost
from biff.daemon.session_pointer import CurrentPointer
from biff.daemon.session_select import SessionSelectionError, SessionSelector
from biff.daemon.session_store import (
    CliSessionEndedError,
    CliSessionRecord,
    CliSessionStore,
)
from biff.models import BiffConfig, Message, UserSession
from biff.relay import LocalRelay
from biff.session_id import CLAUDE_CODE_ROLE, CLI_ROLE, derive_routing_id
from biff.session_routing import SessionRouting

HOST = "host1"


def _host(
    tmp_path: Path, *, user: str = "kai", repo_root: Path | None = None
) -> SessionHost:
    root = repo_root or tmp_path / "repo"
    root.mkdir(exist_ok=True)
    selector = SessionSelector(
        store=CliSessionStore(tmp_path / "sessions"),
        pointer=CurrentPointer(tmp_path / "current"),
        user=user,
        repo="biff",
        repo_root=root,
        hostname=HOST,
    )
    return SessionHost(
        relay=LocalRelay(tmp_path / "relay"),
        config=BiffConfig(user=user, repo_name="biff"),
        selector=selector,
        hostname=HOST,
        pwd=str(root),
    )


class TestResolve:
    async def test_auto_creates_then_reuses_via_pointer(self, tmp_path: Path) -> None:
        host = _host(tmp_path)
        first = await host.resolve(None)
        second = await host.resolve(None)
        assert first.tty_name == second.tty_name == "tty1"
        assert first.routing == second.routing
        assert first.routing.role == CLI_ROLE

    async def test_explicit_name_attaches_that_session(self, tmp_path: Path) -> None:
        host = _host(tmp_path)
        await host.create("alpha")
        await host.create("beta")
        attached = await host.resolve("alpha")
        assert attached.tty_name == "alpha"

    async def test_explicit_id_attaches(self, tmp_path: Path) -> None:
        host = _host(tmp_path)
        record = await host.create("alpha")
        assert (await host.resolve(record.session_id)).tty_name == "alpha"

    async def test_explicit_unknown_is_an_error_and_creates_nothing(
        self, tmp_path: Path
    ) -> None:
        host = _host(tmp_path)
        with pytest.raises(SessionSelectionError, match="unknown session: nope"):
            await host.resolve("nope")
        assert host.selector.store.list() == ()

    async def test_explicit_ended_is_the_named_error(self, tmp_path: Path) -> None:
        host = _host(tmp_path)
        record = await host.create("alpha")
        await host.end(record)
        with pytest.raises(CliSessionEndedError, match="biff session new --tty alpha"):
            await host.resolve("alpha")

    async def test_explicit_wrong_repo_is_an_error(self, tmp_path: Path) -> None:
        host = _host(tmp_path)
        await host.create("alpha")
        other = SessionHost(
            relay=host.relay,
            config=host.config,
            selector=SessionSelector(
                store=host.selector.store,
                pointer=host.selector.pointer,
                user="kai",
                repo="other",
                repo_root=tmp_path / "other",
                hostname=HOST,
            ),
            hostname=HOST,
            pwd="",
        )
        with pytest.raises(SessionSelectionError, match="belongs to repo biff"):
            await other.resolve("alpha")

    async def test_explicit_other_users_session_is_an_error(
        self, tmp_path: Path
    ) -> None:
        host = _host(tmp_path)
        await host.create("alpha")
        eric = _host(tmp_path, user="eric")
        with pytest.raises(SessionSelectionError, match="belongs to kai, not eric"):
            await eric.resolve("alpha")

    async def test_stale_pointer_falls_through_to_a_new_session(
        self, tmp_path: Path
    ) -> None:
        host = _host(tmp_path)
        old = await host.create("alpha")
        await host.end(old)
        fresh = await host.resolve(None)
        assert fresh.record is not None
        assert fresh.record.session_id != old.session_id
        assert host.selector.pointer.read(host.selector.repo_root) == (
            fresh.record.session_id
        )

    async def test_harness_session_is_attached_as_a_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "harness-1")
        host = _host(tmp_path)
        await host.relay.update_session(
            UserSession(
                user="kai",
                tty=SessionRouting("harness-1").routing_id,
                tty_name="tty7",
                repo="biff",
            )
        )
        attached = await host.resolve(None)
        assert attached.record is None
        assert attached.routing == SessionRouting("harness-1")
        assert attached.routing.routing_id == derive_routing_id(
            "harness-1", CLAUDE_CODE_ROLE
        )
        # No CLI record, no pointer, no second presence row.
        assert host.selector.store.list() == ()
        assert host.selector.pointer.read(host.selector.repo_root) is None
        assert len(await host.relay.get_sessions()) == 1

    async def test_explicit_beats_harness(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host = _host(tmp_path)
        await host.create("alpha")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "harness-1")
        attached = await host.resolve("alpha")
        assert attached.record is not None


class TestCreate:
    async def test_registers_row_and_name_and_leaves_the_pointer(
        self, tmp_path: Path
    ) -> None:
        host = _host(tmp_path)
        record = await host.create("main")
        key = SessionRouting(record.session_id, role=CLI_ROLE).session_key("kai")
        row = await host.relay.get_session(key)
        assert row is not None
        assert row.tty_name == "main"
        assert await host.relay.get_tty_reservation_owner("kai", "main") == key
        assert host.selector.pointer.read(host.selector.repo_root) is None

    async def test_taken_exact_name_errors_and_creates_nothing(
        self, tmp_path: Path
    ) -> None:
        host = _host(tmp_path)
        await host.create("main")
        with pytest.raises(ValueError, match="already in use"):
            await host.create("main")
        assert len(host.selector.store.list()) == 1

    async def test_invalid_name_is_refused(self, tmp_path: Path) -> None:
        host = _host(tmp_path)
        with pytest.raises(ValueError, match="Invalid tty name"):
            await host.create("bad name")
        assert host.selector.store.list() == ()

    async def test_names_are_lowest_free(self, tmp_path: Path) -> None:
        host = _host(tmp_path)
        assert (await host.create()).tty == "tty1"
        assert (await host.create()).tty == "tty2"


class TestAttach:
    async def test_attach_revives_a_dormant_session_with_its_inbox(
        self, tmp_path: Path
    ) -> None:
        host = _host(tmp_path)
        record = await host.create("alpha")
        host.selector.store.lapse(record.session_id)
        key = SessionRouting(record.session_id, role=CLI_ROLE).session_key("kai")
        await host.relay.deliver(
            Message(from_user="eric", to_user=key, body="while you were out")
        )
        attached = await host.resolve("alpha")
        assert attached.record is not None
        assert attached.record.state == "live"
        assert [m.body for m in await host.relay.fetch(key)] == ["while you were out"]

    async def test_attach_marks_the_command_as_activity(self, tmp_path: Path) -> None:
        host = _host(tmp_path)
        record = await host.create("alpha")
        key = SessionRouting(record.session_id, role=CLI_ROLE).session_key("kai")
        row = await host.relay.get_session(key)
        assert row is not None
        stale = datetime.now(UTC) - timedelta(hours=1)
        await host.relay.update_session(row.model_copy(update={"last_tool_at": stale}))
        await host.resolve("alpha")
        attached = await host.relay.get_session(key)
        assert attached is not None
        assert attached.last_tool_at > stale + timedelta(minutes=59)


class TestEnd:
    async def test_end_drains_unread_mail_into_the_user_mailbox(
        self, tmp_path: Path
    ) -> None:
        host = _host(tmp_path)
        record = await host.create("alpha")
        key = SessionRouting(record.session_id, role=CLI_ROLE).session_key("kai")
        for body in ("one", "two"):
            await host.relay.deliver(
                Message(from_user="eric", from_tty="tty9", to_user=key, body=body)
            )

        ended = await host.end(record)

        assert ended.state == "ended"
        mailbox = await host.relay.fetch_user_inbox("kai")
        assert sorted(m.body for m in mailbox) == ["one", "two"]
        assert {m.from_user for m in mailbox} == {"eric"}
        assert await host.relay.fetch(key) == []

    async def test_end_releases_name_and_row(self, tmp_path: Path) -> None:
        host = _host(tmp_path)
        record = await host.create("alpha")
        key = SessionRouting(record.session_id, role=CLI_ROLE).session_key("kai")
        await host.end(record)
        assert await host.relay.get_session(key) is None
        assert await host.relay.get_tty_reservation_owner("kai", "alpha") is None

    async def test_name_can_be_reused_by_a_new_session_after_end(
        self, tmp_path: Path
    ) -> None:
        host = _host(tmp_path)
        old = await host.create("alpha")
        await host.end(old)
        new = await host.create("alpha")
        assert new.session_id != old.session_id
        attached = await host.resolve("alpha")
        assert attached.record is not None
        assert attached.record.session_id == new.session_id

    async def test_reused_name_does_not_inherit_the_dead_sessions_mail(
        self, tmp_path: Path
    ) -> None:
        host = _host(tmp_path)
        old = await host.create("alpha")
        old_key = SessionRouting(old.session_id, role=CLI_ROLE).session_key("kai")
        await host.end(old)
        new = await host.create("alpha")
        new_key = SessionRouting(new.session_id, role=CLI_ROLE).session_key("kai")
        assert new_key != old_key
        await host.relay.deliver(
            Message(from_user="eric", to_user=old_key, body="to the dead one")
        )
        assert await host.relay.fetch(new_key) == []


class TestSweep:
    async def test_idle_live_session_lapses_to_dormant(self, tmp_path: Path) -> None:
        host = _host(tmp_path)
        record = await host.create("alpha")
        store = host.selector.store
        old = datetime.now(UTC) - LEASE - timedelta(minutes=1)
        _backdate(store, record.session_id, last_attached_at=old)
        await host.sweep()
        assert store.read(record.session_id).state == "dormant"

    async def test_recent_session_is_untouched(self, tmp_path: Path) -> None:
        host = _host(tmp_path)
        record = await host.create("alpha")
        await host.sweep()
        assert host.selector.store.read(record.session_id).state == "live"

    async def test_expiry_drains_mail_and_removes_the_record(
        self, tmp_path: Path
    ) -> None:
        host = _host(tmp_path)
        record = await host.create("alpha")
        key = SessionRouting(record.session_id, role=CLI_ROLE).session_key("kai")
        await host.relay.deliver(
            Message(from_user="eric", to_user=key, body="stranded?")
        )
        store = host.selector.store
        store.lapse(record.session_id)
        old = datetime.now(UTC) - RETENTION - timedelta(minutes=1)
        _backdate(store, record.session_id, last_attached_at=old)

        await host.sweep()

        assert store.list() == ()
        assert [m.body for m in await host.relay.fetch_user_inbox("kai")] == [
            "stranded?"
        ]
        assert await host.relay.get_tty_reservation_owner("kai", "alpha") is None


def _backdate(
    store: CliSessionStore, session_id: str, *, last_attached_at: datetime
) -> None:
    record = dataclasses.replace(
        store.read(session_id), last_attached_at=last_attached_at
    )
    (store.root / session_id / "session.json").write_text(record.to_json())


class TestKeyIsTheRecordsDerivedKey:
    async def test_pointer_rung_resolves_to_the_records_own_key_every_time(
        self, tmp_path: Path
    ) -> None:
        host = _host(tmp_path)
        record = await host.create("demo1")
        host.selector.pointer.write(host.selector.repo_root, record.session_id)
        expected = SessionRouting(record.session_id, role=CLI_ROLE).session_key("kai")

        first = await host.resolve(None)
        second = await host.resolve(None)

        assert first.routing.session_key("kai") == expected
        assert second.routing.session_key("kai") == expected
        assert first.tty_name == second.tty_name == "demo1"
        assert len(host.selector.store.list()) == 1

    async def test_harness_session_with_no_row_for_this_user_falls_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Inside Claude Code the harness session is registered under the
        # agent's identity; a CLI run as another user has no row to attach to.
        # That is a missing target, so the pointer rung must take over.
        host = _host(tmp_path)
        record = await host.create("demo1")
        host.selector.pointer.write(host.selector.repo_root, record.session_id)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "harness-1")

        attached = await host.resolve(None)

        assert attached.record is not None
        assert attached.record.session_id == record.session_id
        assert attached.tty_name == "demo1"
        assert attached.routing.session_key("kai") == (
            SessionRouting(record.session_id, role=CLI_ROLE).session_key("kai")
        )

    async def test_harness_session_with_a_row_is_still_a_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host = _host(tmp_path)
        harness = SessionRouting("harness-1")
        await host.relay.update_session(
            UserSession(
                user="kai",
                tty=harness.routing_id,
                tty_name="tty7",
                repo="biff",
            )
        )
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "harness-1")

        attached = await host.resolve(None)

        assert attached.record is None
        assert attached.tty_name == "tty7"
        assert host.selector.store.list() == ()


class _FailingMailbox(LocalRelay):
    """LocalRelay whose user-mailbox write fails on the Nth delivery."""

    def __init__(self, data_dir: Path, *, fail_on: int) -> None:
        super().__init__(data_dir)
        self._fail_on = fail_on
        self._mailbox_writes = 0

    async def deliver(
        self,
        message: Message,
        *,
        sender_key: str = "",
        target_repo: str | None = None,
    ) -> None:
        if ":" not in message.to_user:
            self._mailbox_writes += 1
            if self._mailbox_writes == self._fail_on:
                msg = "mailbox write failed"
                raise ConnectionError(msg)
        await super().deliver(message, sender_key=sender_key, target_repo=target_repo)


class _ConsumingFetch(_FailingMailbox):
    """Models the NATS work queue: fetch removes what it returns."""

    consumes_on_fetch = True

    async def fetch(self, session_key: str) -> list[Message]:
        unread = await super().fetch(session_key)
        await self.mark_read(session_key, [m.id for m in unread])
        return unread


class _UnconfirmedAck(_ConsumingFetch):
    """A consuming relay whose ack of one body is unconfirmed: it stays queued once."""

    unconfirmed = "two"

    def __init__(self, data_dir: Path, *, fail_on: int) -> None:
        super().__init__(data_dir, fail_on=fail_on)
        self._queued_once = True

    async def fetch(self, session_key: str) -> list[Message]:
        unread = await _FailingMailbox.fetch(self, session_key)
        keep = [m for m in unread if m.body == self.unconfirmed and self._queued_once]
        self._queued_once = False
        await LocalRelay.mark_read(
            self, session_key, [m.id for m in unread if m not in keep]
        )
        return unread

    async def mark_read(self, session_key: str, ids: Sequence[uuid.UUID]) -> None:
        """Do nothing: the NATS relay consumed the mail at fetch."""


def _with_relay(host: SessionHost, relay: LocalRelay) -> SessionHost:
    return dataclasses.replace(host, relay=relay)


class TestDrainFailure:
    async def test_restore_loss_logs_session_id_without_login(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        relay = MagicMock(spec=LocalRelay)
        relay.fetch = AsyncMock(return_value=[])
        relay.deliver = AsyncMock(side_effect=OSError("restore failed"))
        await InboxDrain(relay)._restore(
            "private-login:safe-session-id",
            [
                Message(
                    from_user="sender",
                    to_user="private-login:safe-session-id",
                    body="hi",
                )
            ],
            set(),
        )
        assert "Lost 1 (tty=safe-session-id)" in caplog.text
        assert "private-login" not in caplog.text

    async def test_a_batch_of_consumed_and_unconfirmed_mail_loses_nothing(
        self, tmp_path: Path
    ) -> None:
        """Forwarding fails on a batch where "two" is still queued, "three" is not."""
        base = _host(tmp_path)
        record = await base.create("alpha")
        host = _with_relay(base, _UnconfirmedAck(tmp_path / "relay", fail_on=2))
        key = SessionRouting(record.session_id, role=CLI_ROLE).session_key("kai")
        for body in ("one", "two", "three"):
            await host.relay.deliver(Message(from_user="eric", to_user=key, body=body))

        with pytest.raises(ConnectionError):
            await host.end(record)

        in_mailbox = [m.body for m in await host.relay.fetch_user_inbox("kai")]
        in_session = [m.body for m in await host.relay.fetch(key)]
        assert in_mailbox == ["one"]
        assert sorted(in_session) == ["three", "two"]

    async def test_a_retry_after_an_unconfirmed_forward_makes_one_copy(
        self, tmp_path: Path
    ) -> None:
        """The ack of forwarded "one" is unconfirmed; the drain then fails on "two"."""

        class OneUnconfirmed(_UnconfirmedAck):
            unconfirmed = "one"

        base = _host(tmp_path)
        record = await base.create("alpha")
        host = _with_relay(base, OneUnconfirmed(tmp_path / "relay", fail_on=2))
        key = SessionRouting(record.session_id, role=CLI_ROLE).session_key("kai")
        for body in ("one", "two"):
            await host.relay.deliver(Message(from_user="eric", to_user=key, body=body))

        with pytest.raises(ConnectionError):
            await host.end(record)
        await host.end(record)

        in_mailbox = [m.body for m in await host.relay.fetch_user_inbox("kai")]
        in_session = [m.body for m in await host.relay.fetch(key)]
        assert sorted(in_mailbox) == ["one", "two"]
        assert in_session == []

    @pytest.mark.parametrize("relay_cls", [_FailingMailbox, _ConsumingFetch])
    async def test_failed_drain_neither_duplicates_nor_loses(
        self, tmp_path: Path, relay_cls: type[_FailingMailbox]
    ) -> None:
        base = _host(tmp_path)
        record = await base.create("alpha")
        host = _with_relay(base, relay_cls(tmp_path / "relay", fail_on=2))
        key = SessionRouting(record.session_id, role=CLI_ROLE).session_key("kai")
        for body in ("one", "two", "three"):
            await host.relay.deliver(Message(from_user="eric", to_user=key, body=body))

        with pytest.raises(ConnectionError):
            await host.end(record)

        in_mailbox = [m.body for m in await host.relay.fetch_user_inbox("kai")]
        in_session = [m.body for m in await host.relay.fetch(key)]
        assert sorted(in_mailbox + in_session) == ["one", "three", "two"]
        assert in_mailbox == ["one"]
        # The session is not marked ended, so a retry can finish the drain.
        assert host.selector.store.read(record.session_id).state == "live"

    async def test_retry_after_a_failed_drain_completes_it(
        self, tmp_path: Path
    ) -> None:
        base = _host(tmp_path)
        record = await base.create("alpha")
        flaky = _FailingMailbox(tmp_path / "relay", fail_on=2)
        host = _with_relay(base, flaky)
        key = SessionRouting(record.session_id, role=CLI_ROLE).session_key("kai")
        for body in ("one", "two", "three"):
            await host.relay.deliver(Message(from_user="eric", to_user=key, body=body))
        with pytest.raises(ConnectionError):
            await host.end(record)

        await host.end(record)

        assert sorted(m.body for m in await host.relay.fetch_user_inbox("kai")) == [
            "one",
            "three",
            "two",
        ]


@dataclasses.dataclass(frozen=True)
class _ExpiresOnAttach(CliSessionStore):
    """Store whose pointed record vanishes between the pointer read and attach."""

    doomed: str = ""

    def attach(self, session_id: str, *, now: datetime) -> CliSessionRecord:
        if session_id == self.doomed:
            self.lapse(session_id)
            self.expire(session_id)
        return super().attach(session_id, now=now)


class TestPointerRace:
    async def test_pointed_record_removed_before_attach_creates_a_fresh_one(
        self, tmp_path: Path
    ) -> None:
        base = _host(tmp_path)
        gone = await base.create("alpha")
        store = _ExpiresOnAttach(base.selector.store.root, gone.session_id)
        racing = dataclasses.replace(base.selector, store=store)
        host = dataclasses.replace(base, selector=racing)

        attached = await host.resolve(None)

        assert attached.record is not None
        assert attached.record.session_id != gone.session_id
        assert host.selector.pointer.read(host.selector.repo_root) == (
            attached.record.session_id
        )


class _BatchedConsuming(LocalRelay):
    """Models the NATS work queue: fetch returns at most 100 and consumes them."""

    BATCH = 100

    async def fetch(self, session_key: str) -> list[Message]:
        unread = (await super().fetch(session_key))[: self.BATCH]
        await self.mark_read(session_key, [m.id for m in unread])
        return unread


class TestDrainBeyondOneBatch:
    async def test_every_message_past_the_first_batch_reaches_the_mailbox(
        self, tmp_path: Path
    ) -> None:
        base = _host(tmp_path)
        record = await base.create("alpha")
        host = _with_relay(base, _BatchedConsuming(tmp_path / "relay"))
        key = SessionRouting(record.session_id, role=CLI_ROLE).session_key("kai")
        for n in range(250):
            await host.relay.deliver(
                Message(from_user="eric", to_user=key, body=f"m{n:03d}")
            )

        await host.end(record)

        bodies = sorted(m.body for m in await host.relay.fetch_user_inbox("kai"))
        assert bodies == [f"m{n:03d}" for n in range(250)]

    async def test_non_consuming_relay_terminates(self, tmp_path: Path) -> None:
        host = _host(tmp_path)
        record = await host.create("alpha")
        key = SessionRouting(record.session_id, role=CLI_ROLE).session_key("kai")
        for n in range(3):
            await host.relay.deliver(
                Message(from_user="eric", to_user=key, body=f"m{n}")
            )
        await host.end(record)
        assert len(await host.relay.fetch_user_inbox("kai")) == 3


class TestDrainIsIdempotent:
    async def test_redrain_of_a_message_already_in_the_mailbox_adds_no_copy(
        self, tmp_path: Path
    ) -> None:
        # deliver() reached the mailbox but the caller saw an error (lost ack),
        # so the same source message is drained again on retry.
        host = _host(tmp_path)
        record = await host.create("alpha")
        key = SessionRouting(record.session_id, role=CLI_ROLE).session_key("kai")
        msg = Message(from_user="eric", to_user=key, body="once")
        await host.relay.deliver(msg)

        await InboxDrain(host.relay).move(key, "kai")
        await host.relay.deliver(msg)  # the same message shows up again
        await InboxDrain(host.relay).move(key, "kai")

        assert [m.body for m in await host.relay.fetch_user_inbox("kai")] == ["once"]


class TestSweepScope:
    async def test_foreign_repo_records_are_left_for_their_own_sweep(
        self, tmp_path: Path
    ) -> None:
        base = _host(tmp_path)
        record = await base.create("alpha")
        store = base.selector.store
        store.lapse(record.session_id)
        old = datetime.now(UTC) - RETENTION - timedelta(minutes=1)
        _backdate(store, record.session_id, last_attached_at=old)
        key = SessionRouting(record.session_id, role=CLI_ROLE).session_key("kai")
        await base.relay.deliver(
            Message(from_user="eric", to_user=key, body="not yours to drain")
        )
        other = dataclasses.replace(
            base,
            config=BiffConfig(user="kai", repo_name="other"),
        )

        await other.sweep()

        assert store.read(record.session_id).state == "dormant"
        assert await base.relay.get_session(key) is not None

    async def test_unreadable_record_does_not_fail_the_sweep(
        self, tmp_path: Path
    ) -> None:
        host = _host(tmp_path)
        record = await host.create("alpha")
        bad = host.selector.store.root / "cli-0000000000000bad"
        bad.mkdir()
        (bad / "session.json").write_text("garbage")

        await host.sweep()

        assert host.selector.store.read(record.session_id).state == "live"


class TestStaleHandlesAreJudgedOnCurrentState:
    """Sequential, not racing: a handle from before an end or renewal is re-read.

    These pin the re-read-under-lock behavior.  They do not exercise the lock
    itself; ``TestLifecycleLockUnderRealContention`` does, with real threads,
    and ``test_cli_session_store.py::test_concurrent_creates_one_wins`` covers
    the store flock across threads.
    """

    async def test_end_then_late_attach_cannot_resurrect_presence(
        self, tmp_path: Path
    ) -> None:
        host = _host(tmp_path)
        record = await host.create("alpha")
        key = SessionRouting(record.session_id, role=CLI_ROLE).session_key("kai")

        await host.end(record)
        with pytest.raises(CliSessionEndedError):
            await host.attach(record)  # a stale handle from before the end

        assert await host.relay.get_session(key) is None
        assert await host.relay.get_tty_reservation_owner("kai", "alpha") is None

    async def test_second_end_leaves_a_reused_name_alone(self, tmp_path: Path) -> None:
        host = _host(tmp_path)
        old = await host.create("alpha")
        await host.end(old)
        new = await host.create("alpha")
        new_key = SessionRouting(new.session_id, role=CLI_ROLE).session_key("kai")

        await host.end(old)  # a late duplicate end of the first session

        assert await host.relay.get_tty_reservation_owner("kai", "alpha") == new_key
        assert await host.relay.get_session(new_key) is not None

    async def test_sweep_judges_the_current_state_not_the_listing(
        self, tmp_path: Path
    ) -> None:
        host = _host(tmp_path)
        record = await host.create("alpha")
        store = host.selector.store
        _backdate(
            store,
            record.session_id,
            last_attached_at=datetime.now(UTC) - LEASE - timedelta(minutes=1),
        )
        await host.attach(record)  # renewed after it went stale

        await host.sweep()

        assert store.read(record.session_id).state == "live"

    async def test_renew_extends_the_lease_of_an_attached_session(
        self, tmp_path: Path
    ) -> None:
        host = _host(tmp_path)
        record = await host.create("alpha")
        store = host.selector.store
        _backdate(
            store,
            record.session_id,
            last_attached_at=datetime.now(UTC) - LEASE + timedelta(seconds=1),
        )
        await host.renew(record)
        assert store.read(record.session_id).last_attached_at > (
            datetime.now(UTC) - timedelta(minutes=1)
        )


class TestContextActsAsTheRecordsUser:
    async def test_cli_context_uses_the_stored_user(self, tmp_path: Path) -> None:
        host = _host(tmp_path)
        attached = await host.resolve(None)
        ctx = host.context_for(attached)
        assert attached.record is not None
        assert ctx.user == attached.record.user == "kai"
        assert ctx.session_key == (
            SessionRouting(attached.record.session_id, role=CLI_ROLE).session_key("kai")
        )


class _SlowRelay(LocalRelay):
    """LocalRelay whose calls yield for a while, widening every race window.

    A host method only interleaves with another where it awaits the relay, so
    without latency each runs to completion before the other starts.
    """

    FETCH_DELAY = 0.3  # a release is slow, so an attach can land inside it
    WRITE_DELAY = 0.4  # a row write that lands after a concurrent end's delete

    async def fetch(self, session_key: str) -> list[Message]:
        await asyncio.sleep(self.FETCH_DELAY)
        return await super().fetch(session_key)

    async def update_session(self, session: UserSession) -> None:
        await asyncio.sleep(self.WRITE_DELAY)
        await super().update_session(session)


def _race(
    host: SessionHost,
    relay_dir: Path,
    *jobs: tuple[float, Callable[[SessionHost], Coroutine[object, object, object]]],
) -> list[object]:
    """Run each job in its own thread and event loop, released together.

    Every thread gets its own ``SessionHost`` over the same store and relay
    directories, which is what two ``biff`` processes look like.  Each job
    starts after its own head start so the order of the first steps is fixed
    and only the lock decides what happens next.
    """
    barrier = threading.Barrier(len(jobs))
    results: list[object] = [None] * len(jobs)

    def run(
        i: int,
        head_start: float,
        job: Callable[[SessionHost], Coroutine[object, object, object]],
    ) -> None:
        mine = dataclasses.replace(host, relay=_SlowRelay(relay_dir))
        barrier.wait()
        time.sleep(head_start)
        try:
            results[i] = asyncio.run(job(mine))
        except (CliSessionEndedError, SessionSelectionError, OSError) as exc:
            results[i] = exc

    threads = [
        threading.Thread(target=run, args=(i, head, job))
        for i, (head, job) in enumerate(jobs)
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    return results


class TestLifecycleLockUnderRealContention:
    """Two OS threads, each with its own event loop, contend on the session lock.

    Neutering ``SessionLock`` turns each of these red; the lock is the guard.
    """

    def test_attach_racing_end_never_leaves_an_ended_session_with_a_live_row(
        self, tmp_path: Path
    ) -> None:
        host = _host(tmp_path)
        record = asyncio.run(host.create("alpha"))
        key = SessionRouting(record.session_id, role=CLI_ROLE).session_key("kai")

        # attach moves the record to live, then writes its row slowly; end
        # starts while that write is pending.
        results = _race(
            host,
            tmp_path / "relay",
            (0.0, lambda h: h.attach(record)),
            (0.05, lambda h: h.end(record)),
        )

        state = host.selector.store.read(record.session_id).state
        row = asyncio.run(host.relay.get_session(key))
        owner = asyncio.run(host.relay.get_tty_reservation_owner("kai", "alpha"))
        assert state == "ended", results
        assert row is None, "an ended session must not keep a live presence row"
        assert owner is None
        assert not [
            r
            for r in results
            if isinstance(r, Exception) and not isinstance(r, CliSessionEndedError)
        ]

    def test_attach_racing_expiry_never_strands_a_revived_session(
        self, tmp_path: Path
    ) -> None:
        host = _host(tmp_path)
        record = asyncio.run(host.create("alpha"))
        store = host.selector.store
        key = SessionRouting(record.session_id, role=CLI_ROLE).session_key("kai")
        store.lapse(record.session_id)
        _backdate(
            store,
            record.session_id,
            last_attached_at=datetime.now(UTC) - RETENTION - timedelta(minutes=1),
        )

        # The sweep finds the dormant record past retention and starts to
        # release it (slowly); an attach revives it in the meantime.
        _race(
            host,
            tmp_path / "relay",
            (0.0, lambda h: h.sweep()),
            (0.1, lambda h: h.attach(record)),
        )

        remaining = store.list()
        row = asyncio.run(host.relay.get_session(key))
        owner = asyncio.run(host.relay.get_tty_reservation_owner("kai", "alpha"))
        if remaining:
            (live,) = remaining
            assert live.state == "live"
            assert row is not None, "a live session lost its presence row"
            assert owner == key, "a live session lost its tty name"
        else:
            assert row is None
            assert owner is None
