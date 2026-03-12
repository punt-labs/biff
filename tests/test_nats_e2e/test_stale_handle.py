"""Regression test for DES-029: stale JetStream/KV handle recovery.

Simulates the exact failure mode where the TCP connection drops but cached
``_js`` and ``_kv`` handles remain non-None.  Before the fix, the fast path
in ``_ensure_connected()`` would return these stale handles, causing
``ConnectionClosedError`` on the next tool call.
"""

from __future__ import annotations

import pytest

from biff.nats_relay import NatsRelay

pytestmark = pytest.mark.nats


class TestStaleHandleRecovery:
    """DES-029: _ensure_connected() must detect dead connections."""

    async def test_reconnects_after_closed_connection(self, nats_server: str) -> None:
        """Close _nc directly while _js/_kv are cached; next op must succeed."""
        relay = NatsRelay(
            url=nats_server,
            name="test-stale",
            repo_name="_test-stale",
            stream_prefix="biff-stale",
        )

        # Warm the cache — first call provisions infrastructure.
        js1, kv1 = await relay._ensure_connected()
        assert js1 is not None
        assert kv1 is not None
        assert relay._nc is not None
        assert not relay._nc.is_closed

        # Simulate connection death: close _nc without clearing _js/_kv.
        # This is exactly what happens when the TCP connection drops —
        # nats-py sets is_closed=True but the relay's cached handles persist.
        nc_old = relay._nc
        await nc_old.close()
        assert nc_old.is_closed
        # The disconnected_cb may have cleared _js/_kv, but even if not,
        # the fast-path guard in _ensure_connected() must catch it.

        # Next call must reconnect, not return stale handles.
        js2, kv2 = await relay._ensure_connected()
        assert js2 is not None
        assert kv2 is not None
        nc_new = relay._nc
        assert nc_new is not None
        assert not nc_new.is_closed

        # Verify the new handles actually work — write and read a KV entry.
        await kv2.put("_test-stale.health", b"ok")
        entry = await kv2.get("_test-stale.health")
        assert entry.value == b"ok"

        # Cleanup
        await nc_new.close()
