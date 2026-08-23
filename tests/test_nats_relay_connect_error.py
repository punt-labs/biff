"""Unit tests for connect-failure secret hygiene in ``_open_connection``.

Every call site that expands ``RelayAuth.as_nats_kwargs()`` into
``nats.connect()`` must raise a redacted ``RelayConnectError`` whose
``__context__`` is genuinely ``None`` -- not merely display-suppressed via
``from None`` -- and must clear the auth kwargs dict in a ``finally`` block
regardless of outcome (docs/relay-env-overrides.md Sec 5 item 6).

These tests mock the connection, so they run in tiers 1-2 (no ``nats``
marker, no real server).
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from biff.models import RelayAuth, RelayConnectError
from biff.nats_relay import NatsRelay


def _fake_nc() -> MagicMock:
    nc = MagicMock()
    nc.is_closed = False
    nc.close = AsyncMock()
    return nc


class TestConnectFailureRaisesRelayConnectError:
    @pytest.mark.anyio()
    async def test_connect_failure_raises_redacted_error(self) -> None:
        relay = NatsRelay(
            url="tls://relay.example.com:4222",
            auth=RelayAuth(token="s3cret-token"),
            repo_name="test",
        )
        connect = AsyncMock(side_effect=TimeoutError("nats: timeout"))
        with (
            patch("biff.nats_relay.nats.connect", connect),
            pytest.raises(RelayConnectError, match=re.escape("relay.example.com:4222")),
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
    async def test_redacted_error_message_never_contains_userinfo(self) -> None:
        """A credential-bearing URL must not appear verbatim in the message.

        BIFF_RELAY_URL can in principle carry ``user:pass@`` even though
        docs/relay-env-overrides.md Sec 1 tells operators never to embed it
        there -- the error message must not trust that operators comply.
        """
        relay = NatsRelay(
            url="tls://opuser:opsecret@relay.example.com:4222",
            repo_name="test",
        )
        connect = AsyncMock(side_effect=TimeoutError("nats: timeout"))
        with patch("biff.nats_relay.nats.connect", connect):
            try:
                await relay._ensure_connected()
            except RelayConnectError as exc:
                assert "opsecret" not in str(exc)
                assert "relay.example.com:4222" in str(exc)
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

    @pytest.mark.anyio()
    async def test_connect_failure_error_has_no_chained_context(self) -> None:
        """RelayConnectError's __context__ is genuinely None, not merely hidden.

        This is the structural fix, not display suppression: the redacted
        error is raised only after the try/except/finally handling the raw
        nats.connect() failure has fully completed, so there is no
        "currently handled exception" left for Python to implicitly chain
        onto. A prior implementation used `raise ... from None` from
        *inside* the except clause, which left __context__ populated and
        walkable by anything that ignores __suppress_context__ (a
        debugger, an APM integration, traceback.format_exception(chain=True)
        called explicitly) -- verified empirically that pattern still
        exposed the original exception's traceback frame.
        """
        relay = NatsRelay(
            url="tls://relay.example.com:4222",
            auth=RelayAuth(token="s3cret-token"),
            repo_name="test",
        )
        connect = AsyncMock(side_effect=TimeoutError("nats: timeout"))
        with (
            patch("biff.nats_relay.nats.connect", connect),
            pytest.raises(RelayConnectError) as exc_info,
        ):
            await relay._ensure_connected()
        assert exc_info.value.__context__ is None


class TestAuthKwargsClearedOnConnectFailure:
    """The finally block empties auth_kwargs so no frame retains the secret."""

    @pytest.mark.anyio()
    async def test_auth_kwargs_cleared_after_failure(self) -> None:
        """Asserts on the actual dict _auth_kwargs() returns, not a mock's copy.

        ``connect.await_args.kwargs`` is a *separate* dict built by the
        mock's own ``**`` expansion at call time -- clearing our
        ``auth_kwargs`` local cannot and does not touch it. Holding a
        reference to the real dict object and asserting *that* object is
        empty is the only assertion that actually proves the ``finally``
        block ran.
        """
        relay = NatsRelay(
            url="tls://relay.example.com:4222",
            auth=RelayAuth(token="s3cret-token"),
            repo_name="test",
        )
        held_auth_kwargs = {"token": "s3cret-token"}
        relay._auth_kwargs = MagicMock(  # type: ignore[method-assign]
            return_value=held_auth_kwargs
        )
        connect = AsyncMock(side_effect=TimeoutError("nats: timeout"))
        with (
            patch("biff.nats_relay.nats.connect", connect),
            pytest.raises(RelayConnectError),
        ):
            await relay._ensure_connected()

        assert connect.await_args is not None
        assert connect.await_args.kwargs["token"] == "s3cret-token"
        assert held_auth_kwargs == {}

    @pytest.mark.anyio()
    async def test_auth_kwargs_cleared_even_on_success(self) -> None:
        """The finally block runs on the success path too -- no leftover dict.

        Regression for PR #386 review round 2: the prior version of this
        test asserted on ``connect.await_args.kwargs`` (the mock's own
        captured copy), which stays populated regardless of whether
        ``finally: auth_kwargs.clear()`` ever ran -- a regression dropping
        that line would still pass. This holds a reference to the real
        ``auth_kwargs`` dict object and asserts on it directly.
        """
        relay = NatsRelay(
            url="tls://relay.example.com:4222",
            auth=RelayAuth(token="s3cret-token"),
            repo_name="test",
        )
        relay._provision = AsyncMock(  # type: ignore[method-assign]
            return_value=(MagicMock(), MagicMock(), MagicMock())
        )
        held_auth_kwargs = {"token": "s3cret-token"}
        relay._auth_kwargs = MagicMock(  # type: ignore[method-assign]
            return_value=held_auth_kwargs
        )
        connect = AsyncMock(return_value=_fake_nc())
        with patch("biff.nats_relay.nats.connect", connect):
            await relay._ensure_connected()

        assert connect.await_args is not None
        assert connect.await_args.kwargs["token"] == "s3cret-token"
        assert held_auth_kwargs == {}
