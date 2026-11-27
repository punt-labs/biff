"""Tests for the CLI session record store."""

from __future__ import annotations

import dataclasses
import json
import stat
import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from biff.daemon.session_store import (
    CliSessionEndedError,
    CliSessionRecord,
    CliSessionStore,
)
from biff.session_routing import SessionIdError
from biff.tty import validate_tty_name

T0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=5)
T2 = T0 + timedelta(minutes=10)


def _record(tty: str = "tty1", now: datetime = T0) -> CliSessionRecord:
    return CliSessionRecord.new(
        tty=tty,
        user="kai",
        repo="biff",
        repo_root="/work/biff",
        hostname="host1",
        now=now,
    )


@pytest.fixture
def store(tmp_path: Path) -> CliSessionStore:
    return CliSessionStore(tmp_path / "sessions")


class TestRecord:
    def test_new_is_live_with_cli_prefixed_id(self) -> None:
        rec = _record()
        assert rec.state == "live"
        assert rec.session_id.startswith("cli-")
        assert len(rec.session_id) == len("cli-") + 16
        int(rec.session_id[4:], 16)
        assert rec.created_at == rec.last_attached_at == T0
        assert rec.ended_at is None

    def test_ids_are_never_equal(self) -> None:
        ids = {_record().session_id for _ in range(50)}
        assert len(ids) == 50

    def test_frozen(self) -> None:
        rec = _record()
        with pytest.raises(dataclasses.FrozenInstanceError):
            rec.tty = "tty2"  # type: ignore[misc]  # proving immutability

    def test_json_round_trip(self) -> None:
        rec = _record()
        assert CliSessionRecord.from_json(rec.to_json()) == rec

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone"):
            _record(now=datetime(2026, 1, 1))

    def test_unknown_state_rejected_on_load(self) -> None:
        data = json.loads(_record().to_json())
        data["state"] = "zombie"
        with pytest.raises(ValueError, match="state"):
            CliSessionRecord.from_json(json.dumps(data))


class TestCreateRead:
    def test_round_trip(self, store: CliSessionStore) -> None:
        rec = _record()
        store.create(rec)
        assert store.read(rec.session_id) == rec

    def test_reused_id_refused(self, store: CliSessionStore) -> None:
        rec = _record()
        store.create(rec)
        with pytest.raises(FileExistsError):
            store.create(rec)

    def test_read_unknown(self, store: CliSessionStore) -> None:
        with pytest.raises(FileNotFoundError):
            store.read("cli-0000000000000000")

    def test_list(self, store: CliSessionStore) -> None:
        a, b = _record("tty1"), _record("tty2")
        store.create(a)
        store.create(b)
        assert {r.session_id for r in store.list()} == {a.session_id, b.session_id}

    def test_list_empty_when_root_absent(self, store: CliSessionStore) -> None:
        assert store.list() == ()

    def test_default_root_under_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        rec = _record()
        CliSessionStore.default().create(rec)
        expected = tmp_path / ".punt-labs/biff/sessions" / rec.session_id
        assert (expected / "session.json").is_file()


class TestFilesystem:
    def test_modes(self, store: CliSessionStore, tmp_path: Path) -> None:
        rec = _record()
        store.create(rec)
        d = tmp_path / "sessions" / rec.session_id
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
        assert stat.S_IMODE((d / "session.json").stat().st_mode) == 0o600

    def test_no_temp_files_left(self, store: CliSessionStore, tmp_path: Path) -> None:
        rec = _record()
        store.create(rec)
        store.attach(rec.session_id, now=T1)
        d = tmp_path / "sessions" / rec.session_id
        assert [p.name for p in d.iterdir()] == ["session.json"]

    def test_write_is_atomic(
        self,
        store: CliSessionStore,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rec = _record()
        store.create(rec)

        def boom(self: Path, target: object) -> Path:
            raise OSError("rename failed")

        monkeypatch.setattr(Path, "replace", boom)
        with pytest.raises(OSError, match="rename failed"):
            store.attach(rec.session_id, now=T1)
        monkeypatch.undo()
        assert store.read(rec.session_id) == rec
        d = tmp_path / "sessions" / rec.session_id
        assert [p.name for p in d.iterdir()] == ["session.json"]

    @pytest.mark.parametrize(
        "bad", ["", "../etc", "a/b", "cli-ab cd", "x" * 129, "cli-\n"]
    )
    def test_malformed_id_rejected_before_path(
        self, store: CliSessionStore, tmp_path: Path, bad: str
    ) -> None:
        with pytest.raises(SessionIdError):
            store.read(bad)
        with pytest.raises(SessionIdError):
            store.attach(bad, now=T1)
        assert not (tmp_path / "sessions").exists()


class TestTransitions:
    def test_attach_live_renews_lease(self, store: CliSessionStore) -> None:
        rec = _record()
        store.create(rec)
        out = store.attach(rec.session_id, now=T1)
        assert out.state == "live"
        assert out.last_attached_at == T1
        assert store.read(rec.session_id) == out

    def test_lapse_live_to_dormant(self, store: CliSessionStore) -> None:
        rec = _record()
        store.create(rec)
        out = store.lapse(rec.session_id)
        assert out.state == "dormant"
        assert out.last_attached_at == T0

    def test_attach_dormant_revives_same_record(self, store: CliSessionStore) -> None:
        rec = _record()
        store.create(rec)
        store.lapse(rec.session_id)
        out = store.attach(rec.session_id, now=T2)
        assert out.state == "live"
        assert out.session_id == rec.session_id
        assert out.last_attached_at == T2

    def test_end_live(self, store: CliSessionStore) -> None:
        rec = _record()
        store.create(rec)
        out = store.end(rec.session_id, now=T1)
        assert out.state == "ended"
        assert out.ended_at == T1

    def test_end_dormant(self, store: CliSessionStore) -> None:
        rec = _record()
        store.create(rec)
        store.lapse(rec.session_id)
        assert store.end(rec.session_id, now=T1).state == "ended"

    def test_end_is_idempotent_and_keeps_first_stamp(
        self, store: CliSessionStore
    ) -> None:
        rec = _record()
        store.create(rec)
        store.end(rec.session_id, now=T1)
        assert store.end(rec.session_id, now=T2).ended_at == T1

    def test_attach_ended_raises_never_revives(self, store: CliSessionStore) -> None:
        rec = _record()
        store.create(rec)
        store.end(rec.session_id, now=T1)
        with pytest.raises(CliSessionEndedError, match="ended at"):
            store.attach(rec.session_id, now=T2)
        assert store.read(rec.session_id).state == "ended"

    def test_ended_error_names_recovery(self, store: CliSessionStore) -> None:
        rec = _record("tty7")
        store.create(rec)
        store.end(rec.session_id, now=T1)
        with pytest.raises(CliSessionEndedError) as exc:
            store.attach(rec.session_id, now=T2)
        assert "biff session new --tty tty7" in str(exc.value)

    def test_lapse_ended_raises(self, store: CliSessionStore) -> None:
        rec = _record()
        store.create(rec)
        store.end(rec.session_id, now=T1)
        with pytest.raises(CliSessionEndedError):
            store.lapse(rec.session_id)

    def test_lapse_dormant_is_noop(self, store: CliSessionStore) -> None:
        rec = _record()
        store.create(rec)
        first = store.lapse(rec.session_id)
        assert store.lapse(rec.session_id) == first

    def test_transitions_on_unknown_raise(self, store: CliSessionStore) -> None:
        sid = "cli-0000000000000000"
        with pytest.raises(FileNotFoundError):
            store.attach(sid, now=T1)
        with pytest.raises(FileNotFoundError):
            store.lapse(sid)
        with pytest.raises(FileNotFoundError):
            store.end(sid, now=T1)
        with pytest.raises(FileNotFoundError):
            store.expire(sid)

    def test_frozen_fields_never_change(self, store: CliSessionStore) -> None:
        rec = _record()
        store.create(rec)
        outs = [
            store.attach(rec.session_id, now=T1),
            store.lapse(rec.session_id),
            store.attach(rec.session_id, now=T2),
            store.end(rec.session_id, now=T2),
        ]
        for out in outs:
            assert (
                out.session_id,
                out.tty,
                out.user,
                out.repo,
                out.repo_root,
                out.hostname,
                out.created_at,
                out.version,
            ) == (
                rec.session_id,
                rec.tty,
                rec.user,
                rec.repo,
                rec.repo_root,
                rec.hostname,
                rec.created_at,
                rec.version,
            )


class TestExpire:
    def test_expire_dormant_removes_directory(
        self, store: CliSessionStore, tmp_path: Path
    ) -> None:
        rec = _record()
        store.create(rec)
        store.lapse(rec.session_id)
        store.expire(rec.session_id)
        assert not (tmp_path / "sessions" / rec.session_id).exists()
        with pytest.raises(FileNotFoundError):
            store.read(rec.session_id)

    def test_expire_ended_removes_directory(
        self, store: CliSessionStore, tmp_path: Path
    ) -> None:
        rec = _record()
        store.create(rec)
        store.end(rec.session_id, now=T1)
        store.expire(rec.session_id)
        assert not (tmp_path / "sessions" / rec.session_id).exists()

    def test_expire_live_refused(self, store: CliSessionStore) -> None:
        rec = _record()
        store.create(rec)
        with pytest.raises(ValueError, match="live"):
            store.expire(rec.session_id)
        assert store.read(rec.session_id) == rec


class TestConcurrency:
    def test_concurrent_end_is_not_clobbered_by_stale_attach(
        self,
        store: CliSessionStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rec = _record()
        store.create(rec)
        other = CliSessionStore(store.root)
        real_read = CliSessionStore.read
        done = threading.Event()

        def end_elsewhere() -> None:
            other.end(rec.session_id, now=T1)

        def read_then_race(self: CliSessionStore, session_id: str) -> CliSessionRecord:
            record = real_read(self, session_id)
            if not done.is_set():
                done.set()
                racer = threading.Thread(target=end_elsewhere)
                racer.start()
                racer.join(timeout=0.5)
                read_then_race.racer = racer  # type: ignore[attr-defined]
            return record

        monkeypatch.setattr(CliSessionStore, "read", read_then_race)
        store.attach(rec.session_id, now=T2)
        read_then_race.racer.join()  # type: ignore[attr-defined]
        monkeypatch.undo()
        assert store.read(rec.session_id).state == "ended"


class TestErrorContract:
    def test_read_unknown_is_file_not_found(self, store: CliSessionStore) -> None:
        with pytest.raises(FileNotFoundError):
            store.read("cli-0000000000000000")

    def test_from_json_missing_key_is_value_error(self) -> None:
        data = json.loads(_record().to_json())
        del data["tty"]
        with pytest.raises(ValueError, match="malformed"):
            CliSessionRecord.from_json(json.dumps(data))

    def test_from_json_bad_value_is_value_error(self) -> None:
        data = json.loads(_record().to_json())
        data["created_at"] = "not-a-date"
        with pytest.raises(ValueError, match="malformed"):
            CliSessionRecord.from_json(json.dumps(data))

    def test_from_json_non_object_is_value_error(self) -> None:
        with pytest.raises(ValueError, match="malformed"):
            CliSessionRecord.from_json("[1, 2]")


def test_non_utc_now_round_trips() -> None:
    tz = timezone(timedelta(hours=5))
    rec = _record(now=datetime(2026, 11, 14, 17, 0, tzinfo=tz))
    assert rec.created_at == T0
    assert rec.created_at.tzinfo is UTC
    assert CliSessionRecord.from_json(rec.to_json()) == rec


def _write_raw(
    store: CliSessionStore, session_id: str, data: dict[str, object]
) -> None:
    d = store.root / session_id
    d.mkdir(parents=True)
    (d / "session.json").write_text(json.dumps(data))


class TestStoreHardening:
    def test_concurrent_creates_one_wins(self, store: CliSessionStore) -> None:
        rec = _record()
        barrier = threading.Barrier(8)
        outcomes: list[str] = []

        def attempt() -> None:
            barrier.wait()
            try:
                store.create(rec)
                outcomes.append("ok")
            except FileExistsError:
                outcomes.append("exists")

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert sorted(outcomes) == ["exists"] * 7 + ["ok"]

    def test_second_create_raises(self, store: CliSessionStore) -> None:
        rec = _record()
        store.create(rec)
        with pytest.raises(FileExistsError):
            store.create(rec)

    def test_expire_with_contending_attach_leaves_no_orphan(
        self, store: CliSessionStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _record()
        store.create(rec)
        store.lapse(rec.session_id)
        errors: list[BaseException] = []
        real_read = CliSessionStore.read
        started: list[threading.Thread] = []

        def attach() -> None:
            try:
                CliSessionStore(store.root).attach(rec.session_id, now=T2)
            except BaseException as exc:  # noqa: BLE001 - recorded for the assert
                errors.append(exc)

        def read_and_contend(self: CliSessionStore, sid: str) -> CliSessionRecord:
            record = real_read(self, sid)
            if not started:
                th = threading.Thread(target=attach)
                started.append(th)
                th.start()
                th.join(timeout=0.3)
            return record

        monkeypatch.setattr(CliSessionStore, "read", read_and_contend)
        store.expire(rec.session_id)
        started[0].join()
        monkeypatch.undo()
        assert not (store.root / rec.session_id).exists()
        assert [type(e) for e in errors] == [FileNotFoundError]

    def test_null_identity_field_rejected(self) -> None:
        data = json.loads(_record().to_json())
        data["tty"] = None
        with pytest.raises(ValueError, match="malformed"):
            CliSessionRecord.from_json(json.dumps(data))

    def test_unsupported_version_rejected(self) -> None:
        data = json.loads(_record().to_json())
        data["version"] = 99
        with pytest.raises(ValueError, match="version"):
            CliSessionRecord.from_json(json.dumps(data))

    def test_attach_and_end_normalize_to_utc(self, store: CliSessionStore) -> None:
        tz = timezone(timedelta(hours=-7))
        rec = _record()
        attached = rec.attached(T1.astimezone(tz))
        ended = attached.ended(T2.astimezone(tz))
        assert attached.last_attached_at.tzinfo is UTC
        assert ended.ended_at is not None
        assert ended.ended_at.tzinfo is UTC
        assert CliSessionRecord.from_json(ended.to_json()) == ended

    def test_list_skips_entry_expired_mid_scan(
        self, store: CliSessionStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a, b = _record("tty1"), _record("tty2")
        store.create(a)
        store.create(b)
        store.lapse(b.session_id)
        real_read = CliSessionStore.read

        def read_expiring(self: CliSessionStore, sid: str) -> CliSessionRecord:
            if sid == b.session_id:
                monkeypatch.undo()
                self.expire(b.session_id)
            return real_read(self, sid)

        monkeypatch.setattr(CliSessionStore, "read", read_expiring)
        assert [r.session_id for r in store.list()] == [a.session_id]

    @pytest.mark.parametrize("bad", ["tty1", "cli-xyz", "cli-" + "g" * 16, "cli-abc"])
    def test_create_rejects_non_cli_id(self, store: CliSessionStore, bad: str) -> None:
        rec = dataclasses.replace(_record(), session_id=bad)
        with pytest.raises(ValueError, match="cli-"):
            store.create(rec)

    def test_read_rejects_embedded_id_mismatch(self, store: CliSessionStore) -> None:
        data = json.loads(_record().to_json())
        _write_raw(store, "cli-aaaaaaaaaaaaaaaa", data)
        with pytest.raises(ValueError, match="id mismatch"):
            store.read("cli-aaaaaaaaaaaaaaaa")


class TestListSkipsUnreadableRecords:
    def test_malformed_record_is_skipped_not_fatal(
        self, store: CliSessionStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        good = _record("tty1")
        store.create(good)
        bad = store.root / "cli-0000000000000bad"
        bad.mkdir()
        (bad / "session.json").write_text("{ not json")

        with caplog.at_level("WARNING", logger="biff.daemon.session_store"):
            listed = store.list()

        assert listed == (good,)
        assert "cli-0000000000000bad" in caplog.text

    def test_newer_schema_record_is_skipped(self, store: CliSessionStore) -> None:
        good = _record("tty1")
        store.create(good)
        newer = _record("tty2")
        store.create(newer)
        path = store.root / newer.session_id / "session.json"
        path.write_text(path.read_text().replace('"version": 1', '"version": 99'))

        assert store.list() == (good,)


def test_every_generated_session_id_is_refused_as_a_tty_name() -> None:
    """The id generator and the name check share one pattern, so they agree."""
    for _ in range(50):
        record = CliSessionRecord.new(
            tty="t", user="kai", repo="r", repo_root="/r", hostname="h", now=T0
        )
        assert "session id" in str(validate_tty_name(record.session_id))
