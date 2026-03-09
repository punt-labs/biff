"""Tests for safe_close — Python 3.14+ SSL teardown suppression."""

from __future__ import annotations

import ssl
from unittest.mock import AsyncMock

import pytest

from biff.nats_relay import safe_close


class TestSafeClose:
    """Verify safe_close suppresses only the specific SSL teardown error."""

    async def test_suppresses_close_notify_error(self) -> None:
        nc = AsyncMock()
        msg = (
            "[SSL: APPLICATION_DATA_AFTER_CLOSE_NOTIFY]"
            " application data after close notify"
        )
        nc.close.side_effect = ssl.SSLError(1, msg)
        await safe_close(nc)  # should not raise
        nc.close.assert_awaited_once()

    async def test_raises_other_ssl_errors(self) -> None:
        nc = AsyncMock()
        msg = "[SSL: CERTIFICATE_VERIFY_FAILED] verify failed"
        nc.close.side_effect = ssl.SSLError(1, msg)
        with pytest.raises(ssl.SSLError, match="VERIFY_FAILED"):
            await safe_close(nc)

    async def test_normal_close(self) -> None:
        nc = AsyncMock()
        await safe_close(nc)
        nc.close.assert_awaited_once()
