"""Unit tests for DisplayQueue — pure synchronous, no I/O."""

from __future__ import annotations

import time

from biff.server.display_queue import DisplayItem, DisplayQueue


def _wall(text: str = "deploy freeze", key: str = "wall:deploy") -> DisplayItem:
    return DisplayItem(kind="wall", text=text, source_key=key)


def _talk(text: str = "@kai: ready", key: str = "talk:kai:ready") -> DisplayItem:
    return DisplayItem(kind="talk", text=text, source_key=key)


class TestEmpty:
    def test_current_returns_none(self) -> None:
        q = DisplayQueue()
        assert q.current() is None

    def test_advance_if_due_returns_false(self) -> None:
        q = DisplayQueue()
        assert q.advance_if_due() is False

    def test_snapshot_returns_empty_list(self) -> None:
        q = DisplayQueue()
        assert q.snapshot() == []

    def test_force_to_front_returns_false(self) -> None:
        q = DisplayQueue()
        assert q.force_to_front("nonexistent") is False

    def test_remove_by_source_key_returns_false(self) -> None:
        q = DisplayQueue()
        assert q.remove_by_source_key("nonexistent") is False

    def test_remove_by_kind_does_not_crash(self) -> None:
        q = DisplayQueue()
        q.remove_by_kind("wall")  # no-op on empty queue


class TestAdd:
    def test_add_returns_true_for_new_item(self) -> None:
        q = DisplayQueue()
        assert q.add(_wall()) is True

    def test_add_returns_false_for_duplicate(self) -> None:
        q = DisplayQueue()
        q.add(_wall())
        assert q.add(_wall()) is False

    def test_different_source_keys_are_not_duplicates(self) -> None:
        q = DisplayQueue()
        q.add(_wall(key="wall:a"))
        assert q.add(_wall(key="wall:b")) is True
        assert len(q.snapshot()) == 2

    def test_first_add_sets_current(self) -> None:
        q = DisplayQueue()
        item = _wall()
        q.add(item)
        assert q.current() is item


class TestRemoveBySourceKey:
    def test_removes_matching_item(self) -> None:
        q = DisplayQueue()
        q.add(_wall(key="a"))
        q.add(_wall(key="b"))
        assert q.remove_by_source_key("a") is True
        assert len(q.snapshot()) == 1
        assert q.snapshot()[0].source_key == "b"

    def test_returns_false_for_nonexistent(self) -> None:
        q = DisplayQueue()
        q.add(_wall(key="a"))
        assert q.remove_by_source_key("z") is False

    def test_adjusts_cursor_when_removing_before_current(self) -> None:
        q = DisplayQueue(turn_duration=0.0)
        q.add(_wall(key="a"))
        q.add(_wall(key="b"))
        q.add(_wall(key="c"))
        # Advance to index 1 ("b")
        q.advance_if_due()
        item = q.current()
        assert item is not None
        assert item.source_key == "b"
        # Remove "a" (index 0, before cursor)
        q.remove_by_source_key("a")
        # Cursor should still point to "b"
        item = q.current()
        assert item is not None
        assert item.source_key == "b"


class TestRemoveByKind:
    def test_removes_all_matching(self) -> None:
        q = DisplayQueue()
        q.add(_talk(key="t1"))
        q.add(_wall(key="w1"))
        q.add(_talk(key="t2"))
        q.remove_by_kind("talk")
        items = q.snapshot()
        assert len(items) == 1
        assert items[0].kind == "wall"

    def test_leaves_other_kinds(self) -> None:
        q = DisplayQueue()
        q.add(_wall(key="w1"))
        q.add(_talk(key="t1"))
        q.remove_by_kind("wall")
        items = q.snapshot()
        assert len(items) == 1
        assert items[0].kind == "talk"

    def test_empties_queue_gracefully(self) -> None:
        q = DisplayQueue()
        q.add(_talk(key="t1"))
        q.remove_by_kind("talk")
        assert q.current() is None
        assert q.snapshot() == []

    def test_cursor_on_removed_item_falls_to_next(self) -> None:
        q = DisplayQueue(turn_duration=3600.0)
        q.add(_wall(key="w1"))
        q.add(_talk(key="t1"))
        q.force_to_front("t1")  # cursor now at index 1 (talk item)
        item = q.current()
        assert item is not None
        assert item.source_key == "t1"
        q.remove_by_kind("talk")
        # Wall item should now be current
        item = q.current()
        assert item is not None
        assert item.source_key == "w1"
        assert len(q.snapshot()) == 1


class TestRotation:
    def test_no_advance_before_turn_duration(self) -> None:
        q = DisplayQueue(turn_duration=3600.0)
        q.add(_wall(key="a"))
        q.add(_wall(key="b"))
        assert q.advance_if_due() is False
        item = q.current()
        assert item is not None
        assert item.source_key == "a"

    def test_advance_after_turn_duration(self) -> None:
        q = DisplayQueue(turn_duration=0.0)
        q.add(_wall(key="a"))
        q.add(_wall(key="b"))
        assert q.advance_if_due() is True
        item = q.current()
        assert item is not None
        assert item.source_key == "b"

    def test_wall_items_cycle(self) -> None:
        q = DisplayQueue(turn_duration=0.0)
        q.add(_wall(key="a"))
        q.add(_wall(key="b"))
        # Advance past both items, should cycle back
        q.advance_if_due()  # a → b
        q.advance_if_due()  # b → a
        item = q.current()
        assert item is not None
        assert item.source_key == "a"

    def test_talk_items_discarded_after_turn(self) -> None:
        q = DisplayQueue(turn_duration=0.0)
        q.add(_talk(key="t1"))
        q.add(_wall(key="w1"))
        # Current is talk at index 0
        item = q.current()
        assert item is not None
        assert item.kind == "talk"
        # Advance discards the talk item
        q.advance_if_due()
        assert len(q.snapshot()) == 1
        item = q.current()
        assert item is not None
        assert item.kind == "wall"

    def test_single_item_does_not_advance(self) -> None:
        q = DisplayQueue(turn_duration=0.0)
        q.add(_wall(key="a"))
        # Single wall item stays put (no cycling needed)
        # advance_if_due returns True because the slot expired,
        # allowing the description to refresh (e.g. updated "expires in")
        q.advance_if_due()
        item = q.current()
        assert item is not None
        assert item.source_key == "a"
        # Still one item
        assert len(q.snapshot()) == 1

    def test_single_talk_item_discarded(self) -> None:
        q = DisplayQueue(turn_duration=0.0)
        q.add(_talk(key="t1"))
        q.advance_if_due()
        assert q.current() is None
        assert q.snapshot() == []

    def test_mixed_rotation_order(self) -> None:
        q = DisplayQueue(turn_duration=0.0)
        q.add(_wall(key="w1"))
        q.add(_talk(key="t1"))
        q.add(_wall(key="w2"))
        # Start at w1
        item = q.current()
        assert item is not None
        assert item.source_key == "w1"
        # Advance to t1
        q.advance_if_due()
        item = q.current()
        assert item is not None
        assert item.source_key == "t1"
        # Advance — t1 is discarded, move to w2
        q.advance_if_due()
        item = q.current()
        assert item is not None
        assert item.source_key == "w2"
        assert len(q.snapshot()) == 2  # only walls remain
        # Advance cycles back to w1
        q.advance_if_due()
        item = q.current()
        assert item is not None
        assert item.source_key == "w1"


class TestForceToFront:
    def test_changes_current_item(self) -> None:
        q = DisplayQueue(turn_duration=3600.0)
        q.add(_wall(key="w1"))
        q.add(_talk(key="t1"))
        item = q.current()
        assert item is not None
        assert item.source_key == "w1"
        q.force_to_front("t1")
        item = q.current()
        assert item is not None
        assert item.source_key == "t1"

    def test_returns_false_when_not_found(self) -> None:
        q = DisplayQueue()
        q.add(_wall(key="w1"))
        assert q.force_to_front("nonexistent") is False

    def test_resets_slot_timer(self) -> None:
        q = DisplayQueue(turn_duration=0.0)
        q.add(_wall(key="w1"))
        q.add(_talk(key="t1"))
        # Force talk to front — slot timer resets
        q.force_to_front("t1")
        # Even with turn_duration=0, the reset just happened so
        # monotonic time hasn't advanced enough for another advance.
        # We verify the current item is the forced one.
        item = q.current()
        assert item is not None
        assert item.source_key == "t1"


class TestTimingIntegration:
    """Tests that verify real timing behavior with small durations."""

    def test_advance_respects_real_time(self) -> None:
        q = DisplayQueue(turn_duration=0.05)
        q.add(_wall(key="a"))
        q.add(_wall(key="b"))
        # Should not advance immediately
        assert q.advance_if_due() is False
        # Wait past the turn duration
        time.sleep(0.06)
        assert q.advance_if_due() is True
        item = q.current()
        assert item is not None
        assert item.source_key == "b"

    def test_rapid_talk_messages_queue(self) -> None:
        q = DisplayQueue(turn_duration=0.05)
        q.add(_talk(key="t1", text="first"))
        q.add(_talk(key="t2", text="second"))
        q.add(_talk(key="t3", text="third"))
        # All three are in the queue
        assert len(q.snapshot()) == 3
        # Current is the first one
        item = q.current()
        assert item is not None
        assert item.source_key == "t1"
        # After turn, first is discarded, second shows
        time.sleep(0.06)
        q.advance_if_due()
        item = q.current()
        assert item is not None
        assert item.source_key == "t2"
        assert len(q.snapshot()) == 2
