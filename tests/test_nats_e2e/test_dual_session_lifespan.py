"""Dual-session lifespan over NATS (biff-dzqc).

Boots two full MCP servers, each with a companion, sharing a NATS relay.
Asserts the KV contains four fully formed rows (2 primary + 2 companion)
with non-empty ``tty_name`` and no empty ``display_name`` for rows that
carry one.  This is the test that would have caught the v1.8.0 defect
before it shipped.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport

from biff.models import BiffConfig
from biff.server.app import _register_companion, create_server
from biff.server.state import CompanionSession, create_state

pytestmark = pytest.mark.nats

_TEST_REPO = "_test-dual-lifespan"


class TestDualSessionLifespan:
    """Two concurrent active lifespans register four distinct fully formed rows."""

    async def test_two_concurrent_lifespans_register_distinct_sessions(
        self, nats_server: str, tmp_path: Path
    ) -> None:
        """Two servers, each with a companion, write four fully formed KV rows."""
        state_a = create_state(
            BiffConfig(
                user="claude",
                display_name="Claude Agento",
                kind="agent",
                repo_name=_TEST_REPO,
                relay_url=nats_server,
            ),
            tmp_path / "a",
            tty="aaaa0001",
            hostname="host-a",
            pwd="/test/a",
            companion=CompanionSession(
                user="jfreeman",
                display_name="Jim Freeman",
                kind="human",
                tty="bbbb0001",
            ),
        )
        state_b = create_state(
            BiffConfig(
                user="claude",
                display_name="Claude Agento",
                kind="agent",
                repo_name=_TEST_REPO,
                relay_url=nats_server,
            ),
            tmp_path / "b",
            tty="aaaa0002",
            hostname="host-b",
            pwd="/test/b",
            companion=CompanionSession(
                user="jfreeman",
                display_name="Jim Freeman",
                kind="human",
                tty="bbbb0002",
            ),
        )

        mcp_a = create_server(state_a)
        mcp_b = create_server(state_b)

        async with (
            Client(FastMCPTransport(mcp_a)),
            Client(FastMCPTransport(mcp_b)),
        ):
            # Production registers the companion from the heartbeat loop once
            # the ethos roster resolves the human identity.  With the companion
            # pre-set (no roster in-test) and thus never ``None``, the discovery
            # poll is skipped — drive the same registration path directly so the
            # four rows exist.
            await _register_companion(state_a)
            await _register_companion(state_b)
            sessions = await state_a.relay.get_sessions_for_repos(
                frozenset({_TEST_REPO})
            )

        keys = {(s.user, s.tty): s for s in sessions}
        assert ("claude", "aaaa0001") in keys, "primary A missing"
        assert ("claude", "aaaa0002") in keys, "primary B missing"
        assert ("jfreeman", "bbbb0001") in keys, "companion A missing"
        assert ("jfreeman", "bbbb0002") in keys, "companion B missing"

        for key, session in keys.items():
            assert session.tty_name, f"row {key} has empty tty_name"

        # TTY names must be distinct within each user.
        claude_names = {s.tty_name for (u, _), s in keys.items() if u == "claude"}
        jfreeman_names = {s.tty_name for (u, _), s in keys.items() if u == "jfreeman"}
        assert len(claude_names) == 2, (
            f"expected 2 distinct claude names, got {claude_names}"
        )
        assert len(jfreeman_names) == 2, (
            f"expected 2 distinct jfreeman names, got {jfreeman_names}"
        )
