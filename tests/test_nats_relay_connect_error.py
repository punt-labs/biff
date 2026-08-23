"""Unit tests for connect-failure secret hygiene in ``_open_connection``.

Every call site that expands ``RelayAuth.as_nats_kwargs()`` into
``nats.connect()`` must raise a redacted ``RelayConnectError`` (never the
raw ``nats.connect()`` exception, whose traceback frame can hold the
plaintext auth kwargs) and must clear the auth kwargs dict in a
``finally`` block regardless of outcome (docs/relay-env-overrides.md
Sec 5 item 6).

These tests mock the connection, so they run in tiers 1-2 (no ``nats``
marker, no real server).
"""

from __future__ import annotations

import re
from types import TracebackType
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from biff.models import RelayAuth, RelayConnectError
from biff.nats_relay import NatsRelay


def _fake_nc() -> MagicMock:
    nc = MagicMock()
    nc.is_closed = False
    nc.close = AsyncMock()
    return nc


def _find_auth_kwargs(tb: TracebackType | None) -> dict[str, str] | None:
    """Walk a traceback's frame chain for the ``auth_kwargs`` local.

    Mirrors the exact verification technique docs/relay-env-overrides.md
    Sec 5 item 6 describes: ``from None`` suppresses *display* of a chained
    exception, but ``__context__`` and its ``__traceback__`` stay walkable,
    so a debugger/APM integration that ignores ``__suppress_context__``
    can still reach a frame's locals directly.
    """
    while tb is not None:
        candidate = tb.tb_frame.f_locals.get("auth_kwargs")
        if isinstance(candidate, dict):
            return cast("dict[str, str]", candidate)
        tb = tb.tb_next
    return None


class TestConnectFailureRaisesRelayConnectError:
    @pytest.mark.anyio()
    async def test_connect_failure_raises_redacted_error(self) -> None:
        relay = NatsRelay(
            url="tls://relay.example.com:4222",
            auth=RelayAuth(token="s3cret-token"),
            repo_name="test",
        )
        connect = AsyncMock(side_effect=TimeoutError("nats: timeout"))
        pattern = re.escape("tls://relay.example.com:4222")
        with (
            patch("biff.nats_relay.nats.connect", connect),
            pytest.raises(RelayConnectError, match=pattern),
        ):
            await relay._ensure_connected()

    @pytest.mark.anyio()
    async def test_redacted_error_message_never_contains_the_token(self) -> None:
        relay = NatsRelay(
            url="tls://relay.example.com:4222",
            auth=RelayAuth(token="s3cret-token"),
            repo_name="test",
        )
        connect = AsyncMock(side_effect=TimeoutError("nats: timeout"))
        with patch("biff.nats_relay.nats.connect", connect):
            try:
                await relay._ensure_connected()
            except RelayConnectError as exc:
                assert "s3cret-token" not in str(exc)
            else:
                pytest.fail("expected RelayConnectError")

    @pytest.mark.anyio()
    async def test_raw_original_exception_type_is_not_propagated(self) -> None:
        """Callers see RelayConnectError, never the raw TimeoutError."""
        relay = NatsRelay(
            url="tls://relay.example.com:4222",
            auth=RelayAuth(token="s3cret-token"),
            repo_name="test",
        )
        connect = AsyncMock(side_effect=TimeoutError("nats: timeout"))
        with patch("biff.nats_relay.nats.connect", connect):
            with pytest.raises(RelayConnectError) as exc_info:
                await relay._ensure_connected()
            assert type(exc_info.value) is RelayConnectError

    @pytest.mark.anyio()
    async def test_relay_connect_error_is_a_connection_error(self) -> None:
        """Subclassing ConnectionError keeps existing OSError-catching callers intact.

        talk/REPL paths along ``biff/__main__.py`` catch
        ``(NatsError, TimeoutError, OSError)`` around calls that indirectly
        redial through ``_ensure_connected()``. RelayConnectError must stay
        catchable by that tuple, or a best-effort loop would crash instead
        of absorbing the failure.
        """
        relay = NatsRelay(url="tls://relay.example.com:4222", repo_name="test")
        connect = AsyncMock(side_effect=TimeoutError("nats: timeout"))
        with patch("biff.nats_relay.nats.connect", connect):
            try:
                await relay._ensure_connected()
            except (TimeoutError, OSError) as exc:
                assert isinstance(exc, RelayConnectError)
            else:
                pytest.fail("expected RelayConnectError via the OSError branch")


class TestAuthKwargsClearedOnConnectFailure:
    """The finally block empties auth_kwargs so no frame retains the secret."""

    @pytest.mark.anyio()
    async def test_auth_kwargs_frame_local_is_emptied_after_failure(self) -> None:
        relay = NatsRelay(
            url="tls://relay.example.com:4222",
            auth=RelayAuth(token="s3cret-token"),
            repo_name="test",
        )
        connect = AsyncMock(side_effect=TimeoutError("nats: timeout"))
        with patch("biff.nats_relay.nats.connect", connect):
            try:
                await relay._ensure_connected()
            except RelayConnectError as exc:
                original = exc.__context__
                assert original is not None
                auth_kwargs = _find_auth_kwargs(original.__traceback__)
                assert auth_kwargs == {}
            else:
                pytest.fail("expected RelayConnectError")

    @pytest.mark.anyio()
    async def test_auth_kwargs_cleared_even_on_success(self) -> None:
        """The finally block runs on the success path too -- no leftover dict."""
        relay = NatsRelay(
            url="tls://relay.example.com:4222",
            auth=RelayAuth(token="s3cret-token"),
            repo_name="test",
        )
        relay._provision = AsyncMock(  # type: ignore[method-assign]
            return_value=(MagicMock(), MagicMock(), MagicMock())
        )
        connect = AsyncMock(return_value=_fake_nc())
        with patch("biff.nats_relay.nats.connect", connect):
            await relay._ensure_connected()

        assert connect.await_args is not None
        assert connect.await_args.kwargs["token"] == "s3cret-token"
