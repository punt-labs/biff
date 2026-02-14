"""Tests for JSONL message storage."""

from __future__ import annotations

from pathlib import Path

import pytest

from biff.models import Message
from biff.storage.inbox import MessageStore


@pytest.fixture
def store(tmp_path: Path) -> MessageStore:
    return MessageStore(data_dir=tmp_path)


class TestAppend:
    def test_creates_data_dir(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b"
        store = MessageStore(data_dir=nested)
        msg = Message(from_user="kai", to_user="eric", body="hello")
        store.append(msg)
        assert (nested / "inbox.jsonl").exists()

    def test_append_single(self, store: MessageStore) -> None:
        msg = Message(from_user="kai", to_user="eric", body="hello")
        store.append(msg)
        messages = store._read_all()
        assert len(messages) == 1
        assert messages[0].body == "hello"

    def test_append_multiple(self, store: MessageStore) -> None:
        for i in range(3):
            store.append(Message(from_user="kai", to_user="eric", body=f"msg {i}"))
        assert len(store._read_all()) == 3

    def test_preserves_all_fields(self, store: MessageStore) -> None:
        msg = Message(from_user="kai", to_user="eric", body="auth ready")
        store.append(msg)
        restored = store._read_all()[0]
        assert restored.id == msg.id
        assert restored.from_user == msg.from_user
        assert restored.to_user == msg.to_user
        assert restored.body == msg.body
        assert restored.timestamp == msg.timestamp
        assert restored.read == msg.read


class TestGetUnread:
    def test_empty_store(self, store: MessageStore) -> None:
        assert store.get_unread("eric") == []

    def test_filters_by_recipient(self, store: MessageStore) -> None:
        store.append(Message(from_user="kai", to_user="eric", body="for eric"))
        store.append(Message(from_user="kai", to_user="jess", body="for jess"))
        unread = store.get_unread("eric")
        assert len(unread) == 1
        assert unread[0].body == "for eric"

    def test_excludes_read_messages(self, store: MessageStore) -> None:
        msg = Message(from_user="kai", to_user="eric", body="old")
        store.append(msg)
        store.mark_read([msg.id])
        store.append(Message(from_user="kai", to_user="eric", body="new"))
        unread = store.get_unread("eric")
        assert len(unread) == 1
        assert unread[0].body == "new"

    def test_oldest_first(self, store: MessageStore) -> None:
        store.append(Message(from_user="kai", to_user="eric", body="first"))
        store.append(Message(from_user="kai", to_user="eric", body="second"))
        unread = store.get_unread("eric")
        assert unread[0].body == "first"
        assert unread[1].body == "second"


class TestMarkRead:
    def test_marks_specific_messages(self, store: MessageStore) -> None:
        m1 = Message(from_user="kai", to_user="eric", body="one")
        m2 = Message(from_user="kai", to_user="eric", body="two")
        store.append(m1)
        store.append(m2)
        store.mark_read([m1.id])
        messages = store._read_all()
        assert messages[0].read is True
        assert messages[1].read is False

    def test_idempotent(self, store: MessageStore) -> None:
        msg = Message(from_user="kai", to_user="eric", body="hello")
        store.append(msg)
        store.mark_read([msg.id])
        store.mark_read([msg.id])
        assert store._read_all()[0].read is True

    def test_preserves_other_fields(self, store: MessageStore) -> None:
        msg = Message(from_user="kai", to_user="eric", body="hello")
        store.append(msg)
        store.mark_read([msg.id])
        updated = store._read_all()[0]
        assert updated.id == msg.id
        assert updated.from_user == msg.from_user
        assert updated.body == msg.body
        assert updated.timestamp == msg.timestamp


class TestGetUnreadSummary:
    def test_empty(self, store: MessageStore) -> None:
        summary = store.get_unread_summary("eric")
        assert summary.count == 0
        assert summary.preview == ""

    def test_single_message(self, store: MessageStore) -> None:
        store.append(Message(from_user="kai", to_user="eric", body="auth ready"))
        summary = store.get_unread_summary("eric")
        assert summary.count == 1
        assert "@kai" in summary.preview
        assert "auth ready" in summary.preview

    def test_multiple_messages(self, store: MessageStore) -> None:
        store.append(Message(from_user="kai", to_user="eric", body="auth ready"))
        store.append(Message(from_user="jess", to_user="eric", body="tests pass"))
        summary = store.get_unread_summary("eric")
        assert summary.count == 2
        assert "@kai" in summary.preview
        assert "@jess" in summary.preview

    def test_preview_truncated(self, store: MessageStore) -> None:
        for i in range(5):
            store.append(
                Message(
                    from_user=f"user{i}",
                    to_user="eric",
                    body="a very long message body that goes on and on",
                )
            )
        summary = store.get_unread_summary("eric")
        assert summary.count == 5
        assert len(summary.preview) <= 80

    def test_ignores_other_users(self, store: MessageStore) -> None:
        store.append(Message(from_user="kai", to_user="jess", body="not for eric"))
        summary = store.get_unread_summary("eric")
        assert summary.count == 0


class TestMalformedData:
    def test_skips_malformed_lines(self, store: MessageStore, tmp_path: Path) -> None:
        msg = Message(from_user="kai", to_user="eric", body="valid")
        store.append(msg)
        inbox = tmp_path / "inbox.jsonl"
        with inbox.open("a") as f:
            f.write("this is not json\n")
        messages = store._read_all()
        assert len(messages) == 1
        assert messages[0].body == "valid"

    def test_skips_blank_lines(self, store: MessageStore, tmp_path: Path) -> None:
        msg = Message(from_user="kai", to_user="eric", body="valid")
        store.append(msg)
        inbox = tmp_path / "inbox.jsonl"
        with inbox.open("a") as f:
            f.write("\n\n\n")
        assert len(store._read_all()) == 1

    def test_missing_file_returns_empty(self, store: MessageStore) -> None:
        assert store._read_all() == []
