"""derive_routing_id: the one deterministic (session_id, role) digest."""

from __future__ import annotations

from biff.session_id import derive_routing_id
from biff.tty import validate_routing_id


class TestDeriveRoutingId:
    """derive == deterministic, injective pairing (spec RID == SESSION x KIND)."""

    def test_deterministic(self) -> None:
        a = derive_routing_id("sid-abc", "human")
        b = derive_routing_id("sid-abc", "human")
        assert a == b

    def test_stable_across_resume(self) -> None:
        """The same session_id (a resume) derives the same companion id."""
        before = derive_routing_id("sid-abc", "eric")
        after = derive_routing_id("sid-abc", "eric")
        assert before == after

    def test_distinct_per_role(self) -> None:
        agent = derive_routing_id("sid-abc", "kai")
        human = derive_routing_id("sid-abc", "eric")
        assert agent != human

    def test_distinct_per_session(self) -> None:
        """A fork (fresh session_id) derives a fresh id — no inheritance."""
        parent = derive_routing_id("sid-parent", "eric")
        fork = derive_routing_id("sid-fork", "eric")
        assert parent != fork

    def test_hex_charset_is_valid_routing_id(self) -> None:
        derived = derive_routing_id("sid-abc", "eric")
        assert validate_routing_id(derived) is None

    def test_no_colon_that_would_collide_with_separator(self) -> None:
        assert ":" not in derive_routing_id("sid-abc", "eric")
